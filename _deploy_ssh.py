"""部署用 SSH 工具：连接阿里云 ECS 8.134.96.9。
用法：
  python _deploy_ssh.py exec "命令"        # 执行单条命令
  python _deploy_ssh.py put本地路径 远端路径   # 上传文件
凭据从环境变量读，不落在文件里。
"""
import os
import sys

import paramiko

HOST = os.environ.get('ECS_HOST', '8.134.96.9')
USER = os.environ.get('ECS_USER', 'root')
PWD = os.environ.get('ECS_PWD')


def client(timeout=20):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PWD, timeout=timeout,
              banner_timeout=30, auth_timeout=30, look_for_keys=False,
              allow_agent=False)
    return c


def run(cmd, timeout=120):
    c = client()
    try:
        _i, out, err = c.exec_command(cmd, timeout=timeout)
        so = out.read().decode('utf-8', 'replace')
        se = err.read().decode('utf-8', 'replace')
        rc = out.channel.recv_exit_status()
        return rc, so, se
    finally:
        c.close()


def put(local, remote):
    c = client()
    try:
        sftp = c.open_sftp()
        sftp.put(local, remote)
        sftp.close()
        print('UPLOADED %s -> %s' % (local, remote))
    finally:
        c.close()


if __name__ == '__main__':
    if not PWD:
        print('缺少 ECS_PWD 环境变量')
        sys.exit(2)
    mode = sys.argv[1]
    if mode == 'exec':
        rc, so, se = run(sys.argv[2])
        if so:
            print(so)
        if se:
            print('--- stderr ---')
            print(se)
        sys.exit(rc)
    elif mode == 'put':
        put(sys.argv[2], sys.argv[3])
    elif mode == 'script':
        # 逐行执行脚本文件里的命令（每行一条，忽略空行与 # 注释）
        with open(sys.argv[2], encoding='utf-8') as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
        rc, so, se = run('\n'.join(lines))
        print(so)
        if se:
            print('--- stderr ---')
            print(se)
        sys.exit(rc)