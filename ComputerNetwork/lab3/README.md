# 计算机网络实践 3：基于原始 TCP 套接字的 SMTP 客户端

不使用 `smtplib`，直接通过 TCP 套接字与邮件服务器交互，依次完成
`EHLO → AUTH LOGIN → MAIL FROM → RCPT TO → DATA → . → QUIT`，
实现一封纯文本邮件的完整投递。

## 环境要求

- Python 3.8 及以上（开发测试环境为 Python 3.13）
- 只用标准库，**不需要安装任何第三方依赖**
- 需要一个支持**明文（非 TLS）SMTP** 的服务器，通常是学校/单位邮箱的 25 端口

> 注意：QQ、163 等公共邮箱的 SMTP 强制使用 TLS（465/587），本客户端无法连接。

## 快速开始

1. 下载或克隆本目录，进入命令行：

   ```bash
   cd ComputerNetwork/lab3
   ```

2. 执行（把地址和服务器换成自己的）：

   ```bash
   python smtp_client.py \
     --host mail.example.edu.cn --port 25 \
     --from 学号@example.edu.cn \
     --to 123456789@qq.com \
     --user 学号@example.edu.cn \
     --subject "计算机网络实践3 SMTP客户端" \
     --body "这是一封测试邮件"
   ```

   Windows PowerShell 的续行符是反引号 `` ` ``，也可以写成一行：

   ```powershell
   python smtp_client.py --host mail.example.edu.cn --port 25 --from 学号@example.edu.cn --to 123456789@qq.com --user 学号@example.edu.cn --subject "计算机网络实践3" --body "测试邮件"
   ```

3. 根据提示输入邮箱密码（输入时不回显）。看到 `邮件已发送给: ...` 即表示投递成功。

## 参数说明

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--host` | 是 | — | SMTP 服务器地址，如 `mail.example.edu.cn` |
| `--port` | 否 | `25` | 服务器端口，需为明文端口 |
| `--from` | 是 | — | 发件人地址（信封发件人，需与登录账号一致，否则可能被拒） |
| `--to` | 是 | — | 收件人地址，可重复写多次实现群发 |
| `--from-name` | 否 | 空 | 发件人显示名称，如 `"张三"` |
| `--subject` | 否 | `SMTP 客户端测试` | 邮件主题，支持中文 |
| `--body` | 否 | 内置测试文本 | 邮件正文，支持中文 |
| `--user` | 否 | 空 | 认证用户名；留空则跳过 `AUTH LOGIN`（匿名投递） |
| `--password` | 否 | 空 | 认证密码；不填则运行时交互输入（推荐，避免密码留在命令历史里） |
| `--timeout` | 否 | `15` | 套接字超时秒数 |
| `--quiet` | 否 | 关闭 | 不打印 `C:`/`S:` 协议往返过程 |

## 运行成功的输出

```
S: 220 ... Anti-spam GT for Coremail System ...
C: EHLO my-pc
S: 250-mail
S: 250-AUTH LOGIN PLAIN
S: 250 8BITMIME
C: AUTH LOGIN
S: 334 dXNlcm5hbWU6
C: <已隐藏>
S: 334 UGFzc3dvcmQ6
C: <已隐藏>
S: 235 Authentication successful
C: MAIL FROM:<学号@example.edu.cn>
S: 250 Mail OK
C: RCPT TO:<123456789@qq.com>
S: 250 Mail OK
C: DATA
S: 354 End data with <CR><LF>.<CR><LF>
C: <邮件报文 512 字节，以 “.” 单独一行结束>
S: 250 Mail OK
C: QUIT
S: 221 Bye
邮件已发送给: 123456789@qq.com
```

`C:` 是客户端发出的命令，`S:` 是服务器响应；账号、密码的 base64 已自动替换为
`<已隐藏>`，可以直接截图用于实验报告。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `getaddrinfo failed` | 服务器域名写错或不存在 | 用 `nslookup 域名` 确认，常见正确形式是 `mail.xxx.edu.cn` |
| 连接超时 / `timed out` | 25 端口被校园网或运营商封锁 | 换用学校邮箱服务器；或按文末方式本地调试 |
| `535 authentication failed` | 账号或密码错误、未开通 SMTP 服务 | 先确认能用网页版登录；邮箱设置里开启 SMTP / 客户端服务；个别系统需要“客户端授权码” |
| `530 authentication required` | 服务器禁止匿名投递 | 必须带 `--user` 认证 |
| `554` / `550` | 被判定为垃圾邮件，或收件人不存在 | 修改主题正文，避免群发；核对收件地址 |
| 程序卡住无输出 | 命令缺少 `\r\n` 或服务器不回 | 一般不会出现，代码已统一补 CRLF；可加 `--timeout 5` 快速失败 |
| 收件端中文乱码 | 客户端未按 MIME 编码 | 本程序已用 `Header` + base64 处理，无需额外设置 |

## 限制

- 仅支持明文 SMTP，不支持 `STARTTLS` 与隐式 TLS（465），因此连接 465 端口会无响应属正常；
- 多个收件人中只要有一个被拒，整次投递即中止；
- 正文固定为 `text/plain` + base64，不支持 HTML 与附件。

## 没有可用 SMTP 服务器时：本地调试

先用标准库起一个只打印报文、不做真实投递的调试服务器：

```bash
pip install aiosmtpd
python -m aiosmtpd -n -l 127.0.0.1:8025 -d
```

另开一个终端发送：

```bash
python smtp_client.py --host 127.0.0.1 --port 8025 \
  --from a@test.com --to b@test.com --subject "test" --body "hello"
```

终端会打印完整的 `220 → 250 → 250 → 354 → 250 → 221` 会话，可用于验证协议流程。

## 提醒

本程序仅用于课程实验与协议学习，请勿用于向他人发送垃圾邮件；请勿在命令行参数中
直接写真实密码（用交互输入），也不要将含有真实凭证的截图公开。
