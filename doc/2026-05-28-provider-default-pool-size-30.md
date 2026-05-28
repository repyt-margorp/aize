# Provider Default Pool Size 30

## User-visible change

Codex, Claude, and Gemini service descriptors now default to 30 service slots each when the runtime starts without provider-specific pool-size environment overrides.

The existing environment overrides remain in place:

- `AIZE_CODEX_POOL_SIZE`
- `AIZE_CLAUDE_POOL_SIZE`
- `AIZE_GEMINI_POOL_SIZE`

## Files touched

- `src/services/codex/service.json`
- `src/services/claude/service.json`
- `src/services/gemini/service.json`
- `tests/test_bootstrap_service_manager.py`

## Verification

```bash
PYTHONPATH=./src python3 -m unittest tests.test_bootstrap_service_manager.BootstrapManifestTests.test_provider_descriptors_default_to_thirty_workers
```
