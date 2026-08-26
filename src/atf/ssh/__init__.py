"""SSH 子包:paramiko 封装执行器。"""

from atf.ssh.executor import CommandResult, SSHExecutor

__all__ = ["SSHExecutor", "CommandResult"]
