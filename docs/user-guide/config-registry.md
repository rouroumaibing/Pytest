# Config Registry

Load YAML with multi-environment overlays, `${VAR}` placeholders, `.env`
injection, and Pydantic validation.

```yaml
# config.yaml
default:
  common:
    api_base: "https://api.example.com"
    retries: 3
  ssh:
    username: "root"
envs:
  staging:
    common:
      api_base: "https://api.staging.example.com"
```

```python
from pydantic import BaseModel
from testkit import ConfigRegistry


class CommonConfig(BaseModel):
    api_base: str
    retries: int


registry = ConfigRegistry("config.yaml", env="staging")
registry.register(["common"], CommonConfig, fixture_name="common")
cfg = registry.get("common")
assert cfg.api_base == "https://api.staging.example.com"
```

## Environment selection

The active environment comes from the `TESTKIT_ENV` variable, or the
`--testkit-env` CLI option (which sets it automatically):

```bash
TESTKIT_ENV=staging pytest ...
pytest --testkit-env staging ...
```

## Placeholders

```yaml
default:
  common:
    api_base: "${API_BASE:-https://default.example.com}"
```

Values are substituted from the environment, with an optional `:-default`
fallback. A missing variable without a default raises `ConfigError`.
