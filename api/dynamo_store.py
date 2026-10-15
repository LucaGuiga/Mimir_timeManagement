"""DynamoDB store with the same interface as store.Store; used on Lambda. One table, keys pk + sk, TTL on expires_epoch."""
import json
import secrets
from datetime import datetime, timezone

LOG_LIMIT = 500
DEFAULT_REGION = "us-west-2"


class DynamoStore:
    def __init__(self, table_name, region=DEFAULT_REGION, failure_ttl_seconds=900):
        import boto3  # in the Lambda runtime; imported late so SQLite-only installs don't need it
        from boto3.dynamodb.conditions import Key
        self._key = Key
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.failure_ttl = int(failure_ttl_seconds)

    def _query(self, **kw):
        items = []
        while True:
            resp = self.table.query(**kw)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp or ("Limit" in kw and len(items) >= kw["Limit"]):
                return items[:kw["Limit"]] if "Limit" in kw else items
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def set_many(self, snapshots):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.table.batch_writer() as w:
            for k, v in snapshots.items():
                w.put_item(Item={"pk": "snap", "sk": k, "payload": json.dumps(v), "updated_at": now})
        return sorted(snapshots)

    def get(self, key):
        item = self.table.get_item(Key={"pk": "snap", "sk": key}, ConsistentRead=True).get("Item")
        return (json.loads(item["payload"]), item["updated_at"]) if item else None

    def get_all_updated_at(self):
        return {i["sk"]: i["updated_at"] for i in self._query(KeyConditionExpression=self._key("pk").eq("snap"), ConsistentRead=True)}

    def replace_passwords(self, entries):
        keep = {e["label"] for e in entries}
        old = self._query(KeyConditionExpression=self._key("pk").eq("pw"), ConsistentRead=True)
        with self.table.batch_writer() as w:
            for i in old:
                if i["sk"] not in keep:
                    w.delete_item(Key={"pk": "pw", "sk": i["sk"]})
            for e in entries:
                w.put_item(Item={"pk": "pw", "sk": e["label"], "salt": e["salt"], "hash": e["hash"]})

    def get_passwords(self):
        items = self._query(KeyConditionExpression=self._key("pk").eq("pw"), ConsistentRead=True)
        return [{"label": i["sk"], "salt": i["salt"], "hash": i["hash"]} for i in items]

    def create_session(self, sid_hash, label, salt, expires_at_epoch):
        self.table.put_item(Item={"pk": f"sess#{sid_hash}", "sk": "-", "label": label, "salt": salt, "expires_epoch": int(expires_at_epoch)})

    def get_session(self, sid_hash):
        item = self.table.get_item(Key={"pk": f"sess#{sid_hash}", "sk": "-"}, ConsistentRead=True).get("Item")
        return {"label": item["label"], "salt": item["salt"], "expires_at": int(item["expires_epoch"])} if item else None

    def delete_session(self, sid_hash):
        self.table.delete_item(Key={"pk": f"sess#{sid_hash}", "sk": "-"})

    @staticmethod
    def _epoch_key(epoch):
        return f"{epoch:017.3f}"

    def record_failure(self, ip, now_epoch):
        sk = f"{self._epoch_key(now_epoch)}#{secrets.token_hex(4)}"
        self.table.put_item(Item={"pk": f"fail#{ip}", "sk": sk, "expires_epoch": int(now_epoch) + self.failure_ttl})

    def count_failures(self, ip, since_epoch):
        kw = {"KeyConditionExpression": self._key("pk").eq(f"fail#{ip}") & self._key("sk").gte(self._epoch_key(since_epoch)), "Select": "COUNT", "ConsistentRead": True}
        total = 0
        while True:
            resp = self.table.query(**kw)
            total += resp["Count"]
            if "LastEvaluatedKey" not in resp:
                return total
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def log_attempt(self, ts_iso, ip, outcome, label):
        item = {"pk": "log", "sk": f"{ts_iso}#{secrets.token_hex(4)}", "ts": ts_iso, "ip": ip, "outcome": outcome}
        if label is not None:
            item["label"] = label
        self.table.put_item(Item=item)

    def access_log_since(self, since_iso):
        cond = self._key("pk").eq("log")
        if since_iso:
            cond = cond & self._key("sk").gt(f"{since_iso}#~")  # '~' sorts after the hex suffix, so equal timestamps are excluded
        items = self._query(KeyConditionExpression=cond, ConsistentRead=True, Limit=LOG_LIMIT)
        return [{"ts": i["ts"], "ip": i["ip"], "outcome": i["outcome"], "label": i.get("label")} for i in items]
