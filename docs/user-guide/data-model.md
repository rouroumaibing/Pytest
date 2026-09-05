# Data Model

Bind domain objects with a fluent `Builder` and parse API responses with
`from_api_response` — keeping the model layer decoupled from the HTTP client.

```python
from testkit import BaseModel, Builder


class Cluster(BaseModel):
    def __init__(self, id, status):
        self.id = id
        self.status = status


class ClusterBuilder(Builder):
    def build(self):
        return Cluster(self._fields["id"], self._fields["status"])


# Fluent construction
cluster = Cluster.builder().with_id("c-1").with_status("Active").build()

# Parse from an API response
data = {"id": "c-1", "status": "Active"}
cluster = Cluster.from_api_response(data)
```

## Conventions

- `Builder` implements `with_<field>` chaining and returns `self`.
- `BaseModel.builder()` returns the associated builder class.
- `from_api_response(data)` maps a raw dict to a model instance.
- The model has **no knowledge** of the HTTP client — keep the two layers
  separate.
