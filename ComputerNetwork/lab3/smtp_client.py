#!/usr/bin/env python3
"""基于原始 TCP 套接字与 SMTP 协议的最小邮件客户端。

不依赖 smtplib：直接与邮件服务器建立 TCP 连接，按课程要求的 SMTP 命令序列
完成 HELO、可选 AUTH LOGIN、MAIL FROM、RCPT TO、DATA 交互，最后 QUIT 关闭连接。
仅支持明文（非 TLS）SMTP，适合连接学校邮箱或自行搭建的明文 SMTP 测试服务器。

用法示例（学校邮箱向 QQ 邮箱发送）：
    python smtp_client.py --host smtp.xxx.edu.cn --port 25 \\
        --from 学号@xxx.edu.cn --to 123456789@qq.com \\
        --user 学号@xxx.edu.cn --subject "计算机网络实践3" --body "测试邮件"
"""

from __future__ import annotations

import argparse
import base64
import getpass
import socket
import sys
from dataclasses import dataclass
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid

# SMTP 规定每条命令、每一行报文都必须以「回车+换行」(CR LF，即 \r\n) 结尾。
# 只写 \n 或忘记换行时，服务器会认为命令尚未发送完毕，表现为「久等不回」。
CRLF = "\r\n"
# 套接字超时（秒）：避免服务器不响应时程序永久卡死，也便于演示超时分支。
DEFAULT_TIMEOUT = 15.0
# 协议交互过程中需要隐藏的敏感内容（账号、密码的 base64 编码）
HIDDEN = "<已隐藏>"


class SMTPError(Exception):
    """SMTP 交互失败，携带服务器返回码、最后一条响应和出错的命令。"""

    def __init__(self, code: int, reply: str, command: str | None = None):
        self.code = code
        self.reply = reply
        self.command = command
        detail = f"SMTP {code}: {reply}" if code else reply
        super().__init__(f"{command} -> {detail}" if command else detail)


def _validate_address(address: str, field_name: str) -> str:
    address = address.strip()
    if not address or "@" not in address or any(ch in address for ch in "\r\n<>"):
        raise ValueError(f"{field_name}不是合法的邮件地址: {address!r}")
    return address


def _validate_header_value(value: str, field_name: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field_name}不能包含换行符，防止邮件头注入")
    return value


@dataclass
class MailMessage:
    """一封纯文本邮件，序列化后即为 SMTP DATA 阶段的报文内容。"""

    sender: str
    recipients: list[str]
    subject: str
    body: str
    sender_name: str | None = None

    def to_bytes(self) -> bytes:
        sender = _validate_address(self.sender, "发件人地址")
        recipients = [_validate_address(r, "收件人地址") for r in self.recipients]
        if not recipients:
            raise ValueError("至少需要一个收件人")
        subject = _validate_header_value(self.subject, "主题")
        if self.sender_name:
            _validate_header_value(self.sender_name, "发件人名称")

        from_value = (
            formataddr((self.sender_name, sender), charset="utf-8")
            if self.sender_name
            else sender
        )
        # 中文主题不能直接写进头部（SMTP 只保证 7 位 ASCII 传输），需按 RFC 2047
        # 编码成 "=?utf-8?b?...?=" 形式；maxlinelen 控制折行长度，避免单行过长。
        subject_value = Header(
            subject, "utf-8", header_name="Subject", maxlinelen=76
        ).encode(linesep=CRLF)
        headers = [
            ("From", from_value),
            ("To", ", ".join(recipients)),
            ("Subject", subject_value),
            ("Date", formatdate(localtime=True)),
            ("Message-ID", make_msgid(domain=sender.rpartition("@")[2])),
            ("MIME-Version", "1.0"),
            ("Content-Type", 'text/plain; charset="utf-8"'),
            ("Content-Transfer-Encoding", "base64"),
        ]
        # 头部每个字段占一行、行间用 CRLF 分隔；头部结束后必须再跟一个空行，
        # 也就是出现连续两个 CRLF，之后的内容才会被服务器当作正文而不是头部。
        header_block = CRLF.join(f"{name}: {value}" for name, value in headers)
        # 正文用 base64 传输：encodebytes 每 76 字符自动插入 \n，这里统一改回 CRLF，
        # 保证整封报文的行尾都是协议要求的 \r\n。
        body_b64 = base64.encodebytes(self.body.encode("utf-8")).decode("ascii")
        body_b64 = body_b64.replace("\n", CRLF).rstrip(CRLF)
        return f"{header_block}{CRLF}{CRLF}{body_b64}{CRLF}".encode("utf-8")


class SMTPClient:
    """一个极简的明文 SMTP 客户端，完整走通 SMTP 会话状态机。

    会话的固定顺序（括号内为服务器应返回的状态码）：
        1. TCP 连接，读问候语 (220)
        2. 收发数据时注意 bytes 与 str 的编码转换
        3. EHLO 自报域名，取扩展能力 (250)，不支持时回退 HELO
        4. AUTH LOGIN 认证 (334 → 334 → 235)
        5. MAIL FROM 声明信封发件人 (250)
        6. RCPT TO 逐个声明收件人 (250/251)
        7. DATA 请求传输报文 (354)
        8. 发送「头部+空行+正文」，以单独一行 "." 结束 (250)
        9. QUIT 结束会话 (221)，关闭 TCP 连接
    任何一步状态码不符合预期都意味着会话状态已乱，继续发送只会得到更多错误，
    因此统一由 _expect() 校验并抛异常终止。
    """

    def __init__(
        self,
        host: str,
        port: int = 25,
        timeout: float = DEFAULT_TIMEOUT,
        trace: bool = True,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.trace = trace  # 是否在终端打印 SMTP 命令往返过程（报告截图用）
        self.capabilities: list[str] = []
        self._socket: socket.socket | None = None
        self._reader = None

    # ---------- 连接生命周期 ----------

    def connect(self) -> "SMTPClient":
        """步骤 1：创建 socket，与邮件服务器建立 TCP 连接。"""
        try:
            # 步骤 1：创建 socket 并与服务器建立 TCP 连接。
            # create_connection 内部先做 DNS 解析（域名写错会报 getaddrinfo failed），
            # 再与目标端口完成三次握手；握手成功后服务器才发来 220 问候语。
            self._socket = socket.create_connection((self.host, self.port), self.timeout)
            self._socket.settimeout(self.timeout)
            # 步骤 2：TCP 只传输字节流，而 SMTP 是按「行」解析的文本协议。
            # makefile("rb") 提供一个带缓冲的按行读取接口：readline() 一定能读到
            # 完整的一行（含行尾 CRLF），既不会读到半行，也避免多行粘连；
            # 拿到 bytes 后再显式 decode，便于处理编码问题。
            self._reader = self._socket.makefile("rb")

            # 服务器在连接建立后主动说话，220 表示「服务就绪」，是会话起点。
            self._expect({220}, "读取服务端问候语")

            # 步骤 3：EHLO 向服务器自报客户端域名，同时索取其支持的扩展能力。
            # 成功响应通常为多行：首行是服务器标识，其余每行是一项能力
            # （AUTH LOGIN、STARTTLS、8BITMIME 等），因此下面要单独解析续行。
            self._send(f"EHLO {socket.getfqdn() or 'localhost'}")
            code, reply = self._read_reply()
            if code == 250:
                # 从第二行开始，去掉每行前 4 个字符（"250-" 或 "250 "）后剩下的
                # 就是能力名；统一转成大写，供 login() 选择认证机制时匹配。
                # 首行只包含服务器标识，因此从 reply.split(CRLF)[1:] 开始取。
                self.capabilities = [
                    line[4:].strip().upper()
                    for line in reply.split(CRLF)[1:]
                    if len(line) > 4
                ]
            elif code in {500, 502, 504}:
                # 老式服务器不认识 EHLO（返回「命令未实现/参数不支持」），
                # 按 RFC 5321 应退回最基础的 HELO，仍可获得 250 后继续会话。
                self._send("HELO localhost")
                self._expect({250}, "HELO")
            else:
                raise SMTPError(code, reply, "EHLO")
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        # 先关读取器再关套接字，并忽略关闭时的 OSError，
        # 保证异常路径下也能安全地释放资源。
        for stream in (self._reader, self._socket):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._reader = None
        self._socket = None

    def quit(self) -> None:
        if self._socket is None:
            return
        try:
            # 步骤 9 的前半程：优雅结束会话。221 表示服务器已接受退出请求；
            # 若服务器已提前断开或返回异常，此处不再抛出，因为连接反正要关闭。
            self._send("QUIT")
            self._expect({221}, "QUIT")
        except (OSError, SMTPError):
            pass  # 服务端可能已提前断开，退出阶段不再抛出
        finally:
            self.close()

    def __enter__(self) -> "SMTPClient":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.quit()

    # ---------- 底层收发与响应解析 ----------

    def _send(self, line: str, secret: bool = False) -> None:
        # 所有命令的唯一出口：统一追加 CRLF，避免每条命令自己拼行尾而漏掉回车。
        # sendall 会保证整行全部写入内核缓冲区，不会出现「只发出去一半」。
        # secret=True 时终端只打印 <已隐藏>，防止截图/日志泄露账号与密码。
        if self.trace:
            print(f"C: {HIDDEN if secret else line}")
        assert self._socket is not None
        self._socket.sendall((line + CRLF).encode("utf-8"))

    def _read_reply(self) -> tuple[int, str]:
        """读取一条（可能是多行的）服务器响应，返回状态码与完整文本。

        服务器响应有两种形态：
            单行：250 OK
            多行：250-mail / 250-AUTH LOGIN / 250 8BITMIME
        多行时前 3 位是状态码，第 4 个字符为 "-" 表示「后面还有」，
        为空格则说明这是最后一行；必须循环读完，否则残留的行会污染下一次读取。
        """
        assert self._reader is not None
        lines: list[str] = []
        while True:
            # readline() 按行读取，返回值含行尾 CRLF；空字节串说明连接已断开。
            raw = self._reader.readline()
            if not raw:
                raise SMTPError(0, "服务器已关闭连接")
            # 网络上传输的是 bytes，这里显式解码成文本并去掉行尾，便于展示与判断。
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if self.trace:
                print(f"S: {line}")
            lines.append(line)
            # line[3] 是状态码后的分隔符："-" 表示续行，非 "-"（通常是空格）表示结束。
            if len(line) < 4 or line[3] != "-":
                break

        # 取最后一行的前三位作为状态码（多行响应的状态码各行一致）。
        code_text = lines[-1][:3]
        if not code_text.isdigit():
            raise SMTPError(0, f"无法解析服务器响应: {lines[-1]!r}")
        return int(code_text), CRLF.join(lines)

    def _expect(self, expected: set[int], command: str) -> str:
        # 用集合是因为个别命令有多个合法成功码，例如 RCPT TO 允许 250 或 251。
        # 状态码不符说明会话已进入非预期状态，立即抛异常并带上命令名，便于定位。
        code, reply = self._read_reply()
        if code not in expected:
            raise SMTPError(code, reply, command)
        return reply

    # ---------- SMTP 命令 ----------

    def login(self, username: str, password: str) -> None:
        """步骤 4：认证身份，成功返回 235。

        AUTH LOGIN 是「一问一答」式：客户端先发 AUTH LOGIN，服务器回 334 并附
        base64 提示串（"username:"），客户端再发 base64(用户名)；服务器再回 334
        （提示 "Password:"），客户端发 base64(密码)，最后服务器回 235 表示通过。
        注意 base64 只是编码、不是加密，明文连接下凭证可被直接还原，这也是实际
        部署必须用 STARTTLS 的原因。
        """
        if not username:
            return  # 未提供账号则跳过认证（匿名/内网中继场景）

        # 从 EHLO 响应中收集服务器支持的认证机制，兼容 “AUTH LOGIN” 与 “AUTH=LOGIN” 两种写法
        mechanisms: set[str] = set()
        for capability in self.capabilities:
            if capability.startswith("AUTH"):
                mechanisms.update(capability[len("AUTH"):].replace("=", " ").split())

        # 优先使用课程要求的 AUTH LOGIN；服务器未声明能力时也尝试一次
        if "PLAIN" in mechanisms and "LOGIN" not in mechanisms:
            # AUTH PLAIN：把 \0用户名\0密码 一次性 base64 编码后随命令发出，只需一次往返。
            token = base64.b64encode(
                f"\0{username}\0{password}".encode("utf-8")
            ).decode("ascii")
            self._send(f"AUTH PLAIN {token}", secret=True)
            self._expect({235}, "AUTH PLAIN")
        else:
            # 第 1 步：请求用 LOGIN 方式认证，期望 334（附带提示串 dXNlcm5hbWU6）。
            self._send("AUTH LOGIN")
            self._expect({334}, "AUTH LOGIN")
            # 第 2 步：发送 base64(用户名)，服务器再回 334 提示输入密码。
            self._send(base64.b64encode(username.encode("utf-8")).decode("ascii"), secret=True)
            self._expect({334}, "AUTH LOGIN 用户名")
            # 第 3 步：发送 base64(密码)，235 表示认证成功；535 表示账号或密码错误。
            self._send(base64.b64encode(password.encode("utf-8")).decode("ascii"), secret=True)
            self._expect({235}, "AUTH LOGIN 密码")

    def send_message(self, sender: str, recipients: list[str], message: bytes) -> list[str]:
        """执行一次完整的邮件投递，返回服务器接受的收件人列表。"""
        sender = _validate_address(sender, "发件人地址")
        recipients = [_validate_address(r, "收件人地址") for r in recipients]
        if not recipients:
            raise ValueError("至少需要一个收件人")

        # 步骤 5：MAIL FROM 指定「信封发件人」，即投递路径上的发送者，也用于退信。
        # 它与报文头里的 From: 是两个概念：前者属于投递层，后者才是收件人看到的内容，
        # 因此理论上可以不同（这正是伪造发件人邮件的来源，实际系统会用 SPF/DKIM 校验）。
        self._send(f"MAIL FROM:<{sender}>")
        self._expect({250}, "MAIL FROM")

        # 步骤 6：RCPT TO 逐个声明收件人，一个地址一条命令、一次响应。
        # 250 表示收件人存在且接受；251 表示「非本地用户，将转发」，同样算成功。
        # 被拒的收件人（如 550 用户不存在）在此处就会暴露，便于及时终止投递。
        accepted: list[str] = []
        for recipient in recipients:
            self._send(f"RCPT TO:<{recipient}>")
            code, reply = self._read_reply()
            if code not in {250, 251}:
                raise SMTPError(code, reply, f"RCPT TO:<{recipient}>")
            accepted.append(recipient)

        # 步骤 7：发送 DATA 请求传输报文。354 是「中间状态」，表示服务器已准备好
        # 接收「头部 + 空行 + 正文」，此时客户端才可以开始发送内容。
        self._send("DATA")
        self._expect({354}, "DATA")
        assert self._socket is not None
        # 步骤 8：一次性发送整封报文（含以 CRLF.CRLF 结尾的结束标记）。
        # 服务器收完后返回 250，表示邮件已被接收并进入投递队列。
        self._socket.sendall(_encode_data(message))
        if self.trace:
            print(f"C: <邮件报文 {len(message)} 字节，以 “.” 单独一行结束>")
        self._expect({250}, "DATA 报文")
        return accepted

    def send(self, message: MailMessage) -> list[str]:
        return self.send_message(message.sender, message.recipients, message.to_bytes())


def _encode_data(message: bytes) -> bytes:
    """把报文体转换为 DATA 阶段要求的字节流：统一 CRLF 并做「点填充」。

    SMTP 用「单独一行 .」表示邮件传输结束，因此正文里凡是以 . 开头的行都必须
    再补一个 .（".hello" → "..hello"），否则服务器会把它误认成结束标记，导致
    邮件内容被截断；接收端读到后会把多出的那个 . 去掉，从而还原原始内容。
    """
    # 先把各种行尾统一成 CRLF，保证协议规定的行格式。
    normalized = message.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    # 报文最后必须以 CRLF 结束，否则结束标记会和正文最后一行粘在一起而不被识别。
    if not normalized.endswith(b"\r\n"):
        normalized += b"\r\n"
    lines = normalized.split(b"\r\n")
    # 对每个以 . 开头的行做点填充（转义），防止被误判为邮件结束标记。
    stuffed = b"\r\n".join(
        b"." + line if line.startswith(b".") else line for line in lines
    )
    # 追加单独一行 "." 作为结束标记，即 CRLF + "." + CRLF。
    return stuffed + b".\r\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="基于原始 TCP 套接字的明文 SMTP 邮件客户端"
    )
    parser.add_argument("--host", required=True, help="SMTP 服务器地址")
    parser.add_argument("--port", type=int, default=25, help="SMTP 端口，默认 25")
    parser.add_argument("--from", dest="sender", required=True, help="发件人地址")
    parser.add_argument(
        "--to", dest="recipients", action="append", required=True, help="收件人地址，可重复指定"
    )
    parser.add_argument("--from-name", default=None, help="发件人显示名称")
    parser.add_argument("--subject", default="SMTP 客户端测试", help="邮件主题")
    parser.add_argument(
        "--body", default="这是一封由原始 TCP 套接字 SMTP 客户端发送的测试邮件。", help="邮件正文"
    )
    parser.add_argument("--user", default=None, help="SMTP 认证用户名，留空则不认证")
    parser.add_argument("--password", default=None, help="SMTP 认证密码，缺省时交互输入")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="套接字超时秒数")
    parser.add_argument("--quiet", action="store_true", help="不打印 SMTP 命令往返过程")
    args = parser.parse_args(argv)

    password = args.password
    if args.user and password is None:
        password = getpass.getpass("SMTP 密码: ")

    message = MailMessage(
        sender=args.sender,
        recipients=args.recipients,
        subject=args.subject,
        body=args.body,
        sender_name=args.from_name,
    )
    try:
        # 一次完整会话：建连(220) → EHLO(250) →（可选）AUTH LOGIN(235)
        #              → MAIL FROM(250) → RCPT TO(250) → DATA(354) → 报文(250)
        #              → QUIT(221) → 关闭 TCP。
        # with 保证无论中途成功还是抛异常，__exit__ 里都会执行 QUIT 并释放套接字；
        # 第四个参数 trace = not args.quiet，默认为 True，即打印 C:/S: 完整往返过程。
        with SMTPClient(args.host, args.port, args.timeout, not args.quiet) as client:
            if args.user:
                client.login(args.user, password or "")
            accepted = client.send(message)
        print(f"邮件已发送给: {', '.join(accepted)}")
        return 0
    except (OSError, SMTPError, ValueError) as exc:
        print(f"发送失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
