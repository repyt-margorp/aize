"""HTTP bridge service handler."""
from runtime.cli_service_adapter import run_http_service

def run_service(**kwargs) -> int:
    return run_http_service(**kwargs)
