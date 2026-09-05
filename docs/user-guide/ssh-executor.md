# SSH Executor

Execute commands over SSH — directly or tunnelled through a jump host via a
`direct-tcpip` channel (no intermediate shell on the bastion).

```python
from testkit import SSHExecutor

# Direct connection
with SSHExecutor("10.0.0.5", username="root", password="secret") as ssh:
    result = ssh.execute("uname -m")
    assert result.ok, result.stderr
    arch = ssh.get_architecture()  # cached "uname -m"

# Jump-host (bastion) connection
with SSHExecutor(
    "10.0.0.5",
    username="root",
    password="secret",
    jump_host="bastion.example.com",
    jump_username="admin",
    jump_password="bastion-secret",
) as ssh:
    result = ssh.execute("systemctl status app")
```

## Result structure

```python
result = ssh.execute("ls /")
print(result.command, result.stdout, result.stderr, result.exit_code, result.duration)
```

## Error handling

```python
from testkit import SSHError

try:
    ssh.execute("exit 1", raise_on_error=True)
except SSHError as err:
    assert err.context["exit_code"] == 1
```

Passwords are never logged — see [Logging](logging.md).
