"""AWS Lambda entry point behind an API Gateway HTTP API. Config comes from MIMIR_* environment variables."""
from mangum import Mangum

from fastapi_app import create_app, load_config_from_env

handler = Mangum(create_app(load_config_from_env()), lifespan="off")
