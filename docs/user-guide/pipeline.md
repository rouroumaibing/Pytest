# Pipeline

Orchestrate serial, resumable multi-stage workflows. Each stage is a
zero-argument callable; a failure stops the pipeline and marks later stages
`skipped`.

```python
from testkit import Pipeline


def provision():
    api.post("/clusters", json={"name": "demo"})


def configure():
    ssh.execute("systemctl enable app")


def verify():
    assert api.get("/clusters/demo").json()["status"] == "Available"


pipeline = Pipeline("deploy")
pipeline.add_stage("provision", provision)
pipeline.add_stage("configure", configure)
pipeline.add_stage("verify", verify)

results = pipeline.run()
assert pipeline.success
```

## Resume from a checkpoint

```python
results = pipeline.run(resume_from="configure")
# "provision" is marked skipped; execution starts at "configure"
```

## Inspecting results

```python
for r in results:
    print(r.name, r.status, r.error)  # status: passed | failed | skipped
```

The `--testkit-resume-from <stage>` CLI option is exposed for passing a resume
point from the command line.
