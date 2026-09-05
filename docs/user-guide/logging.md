# Logging

`--v=N`-style verbosity with sensitive-data sanitization.

## Verbosity levels

| Value | Level | Meaning |
|-------|-------|---------|
| `0` | `INFO` | default |
| `2` | `V2` | API summary (method, path, status) |
| `4` | `V4` | full response body |
| `5` | `V5` | trace detail |

```python
from testkit.logging_setup import setup_logging, get_logger

setup_logging(log_verbosity=2)
logger = get_logger("my.module")
logger.v2("http GET /health -> 200")
```

Enable from the command line:

```bash
pytest --testkit-verbosity 4
```

## Sensitive-data sanitization

Passwords, tokens, and authorization headers are masked automatically:

```python
from testkit.logging_setup import sanitize

sanitize("password=secret")  # "password=***"
sanitize("Authorization: Bearer abc.def.ghi")  # "Authorization: Bearer ***"
sanitize("sshpass -p 'secret' ssh host")  # "sshpass -p '***' ssh host"
```

Both `key=value`/`key: value` forms **and** bare command-line secrets (e.g.
`sshpass -p 'secret'`) are covered.
