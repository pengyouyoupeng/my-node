from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Any
import yaml
import json
import base64
import aiohttp

# ==========================================
# 1. 节点订阅源池与镜像配置
# ==========================================

# GitHub 活跃且持续维护的优质 Clash / Clash.Meta (Mihomo) 节点源池 (专选国内高抗封锁协议)
GITHUB_SOURCES = [
    # --- 持续活跃的大型多协议聚合订阅池 (优先 VLESS / Hysteria2 / Trojan / VMess) ---
    "https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/clash.yml",
    "https://raw.githubusercontent.com/Au1rxx/free-vpn-subscriptions/main/output/clash.yaml",
    "https://raw.githubusercontent.com/anaer/Sub/main/clash.yaml",
    "https://raw.githubusercontent.com/chengaopan/AutoMergePublicNodes/master/list.yml",
    "https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.yml",
    "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta-2.yaml",
    "https://raw.githubusercontent.com/ripaojiedian/freenode/main/clash",
    "https://raw.githubusercontent.com/go4sharing/sub/main/sub.yaml",
]

# GitHub Raw 加速镜像前缀（直连超时或被重置时自动切换）
MIRROR_PREFIXES = [
    "",  # 优先尝试直连
    "https://ghfast.top/",
    "https://mirror.ghproxy.com/",
]

# macOS / Linux 下常见的 mihomo / ClashX Meta / Clash Verge 内核路径
MIHOMO_PATHS = [
    "mihomo",  # 系统环境变量 PATH 中 (如 brew install mihomo)
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo",
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo-alpha",
    "/Applications/ClashX Meta.app/Contents/Resources/clash",
    "/Applications/ClashX Meta.app/Contents/MacOS/clash",
    "/opt/homebrew/bin/mihomo",
    "/usr/local/bin/mihomo",
    os.path.expanduser("~/.config/clash.meta/mihomo"),
]

# 针对中国大陆网络环境：剔除已被 GFW 特征识别并封死的裸 Shadowsocks (SS/SSR)，专选高抗封锁现代协议
SUPPORTED_TYPES = {"vless", "hysteria2", "hysteria", "tuic", "trojan", "vmess"}
CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')

# 测速参数配置（严格把关：2秒内必须有响应，防止半死不活的节点入选）
TEST_URL = "https://www.google.com/generate_204"
TEST_TIMEOUT_MS = 2000    # 缩短至 2 秒，彻底剔除高延迟乌龟节点
API_PORT = 19090          # 独立控制端口，不干扰正在运行的 ClashX Meta (9090)
MAX_PER_C_SUBNET = 1      # 同一个 /24 C段网段最多保留 1 个节点（杜绝集群假节点刷屏）
PRECHECK_LIMIT = 800      # 提高候选池上限，确保能优选出足量节点
FINAL_NODE_COUNT = 30     # 最终输出的节点数量

# 导出与服务配置（自适应当前执行目录，兼容本地 Mac 与云端 Linux GitHub Actions）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "public")
OUTPUT_FILENAME = "clash_meta_30.yaml"
LOCAL_BACKUP_DIR = "/Users/zjs/httpServer"
PROJECT_DIR = BASE_DIR


def get_physical_interface() -> str:
    """自动获取当前默认路由的物理网卡名称，彻底绕过系统 TUN 虚拟网卡（支持 macOS / Linux）"""
    try:
        if sys.platform == "darwin":
            res = subprocess.check_output(["route", "get", "8.8.8.8"], text=True)
            for line in res.splitlines():
                if "interface:" in line:
                    return line.split(":")[1].strip()
        elif sys.platform.startswith("linux"):
            res = subprocess.check_output(["ip", "route", "get", "8.8.8.8"], text=True)
            parts = res.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return ""


def get_c_subnet(server: str) -> str:
    """提取 IPv4 的 C 段 (/24) 或域名作为去重标识"""
    server = server.strip()
    parts = server.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3])  # 如 68.183.227
    return server.lower()


# ==========================================
# 2. 工具与节点清洗函数
# ==========================================

def get_all_sources() -> List[str]:
    """合并静态源与按日期动态生成的每日归档源（自动向前探测最近几天）"""
    dynamic_urls = []
    for days_ago in range(0, 8):
        dt = datetime.now() - timedelta(days=days_ago)
        y, m, d_str = dt.strftime("%Y"), dt.strftime("%m"), dt.strftime("%Y%m%d")
        dynamic_urls.append(
            f"https://raw.githubusercontent.com/clashshare/clashshare.github.io/main/uploads/{y}/{m}/0-{d_str}.yaml"
        )
        dynamic_urls.append(
            f"https://raw.githubusercontent.com/clashshare/clashshare.github.io/main/uploads/{y}/{m}/1-{d_str}.yaml"
        )
    return GITHUB_SOURCES + dynamic_urls


def find_mihomo_binary() -> str:
    """自动寻找本地可用的 mihomo 内核可执行文件"""
    for path in MIHOMO_PATHS:
        resolved = shutil.which(path) if not os.path.isabs(path) else (path if os.access(path, os.X_OK) else None)
        if resolved:
            return resolved
    raise FileNotFoundError(
        "未找到 mihomo 内核！请确认已安装 ClashX Meta.app，或运行 'brew install mihomo' 安装内核。"
    )


def validate_and_clean_node(node: Dict[str, Any]) -> Dict[str, Any] | None:
    """严格清洗节点，防止任何格式畸形导致 mihomo 进程启动失败"""
    if not isinstance(node, dict):
        return None

    ntype = str(node.get("type", "")).lower()
    server = str(node.get("server", "")).strip()
    port = node.get("port")

    if ntype not in SUPPORTED_TYPES or not server or not port:
        return None

    # 过滤无效地址与 IPv6 裸地址格式错误
    if server in ("127.0.0.1", "localhost", "0.0.0.0") or " " in server:
        return None

    try:
        node["port"] = int(port)
        if not (1 <= node["port"] <= 65535):
            return None
    except (ValueError, TypeError):
        return None

    # 必填字段严格校验与抗封锁清洗
    if ntype in ("vmess", "vless", "tuic") and not node.get("uuid"):
        return None
    if ntype == "trojan" and not node.get("password"):
        return None
    if ntype in ("hysteria2", "hysteria") and not (node.get("password") or node.get("auth-str") or node.get("auth_str")):
        return None
    if ntype == "vmess" and "alterId" not in node:
        node["alterId"] = 0

    # 1. Trojan 必须具备合法的域名 SNI 伪装，纯 IP 伪装在国内 5G 环境会被 GFW 探测直接阻断
    if ntype == "trojan":
        sni = str(node.get("sni") or node.get("servername") or "").strip()
        is_ip_server = bool(re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", server))
        if not sni or re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", sni):
            if not is_ip_server:
                node["sni"] = server
                node["servername"] = server
            else:
                return None  # 纯 IP 裸握手 Trojan，大陆无法建立连接，过滤

    # 2. VMess 必须具备 TLS 或 WebSocket/gRPC 传输层伪装，裸 TCP VMess 早已被特征拦截
    if ntype == "vmess":
        net = str(node.get("network", "tcp")).lower()
        tls = bool(node.get("tls"))
        if net == "tcp" and not tls:
            return None

    # 修复与补充 Shadowrocket 兼容性属性
    if node.get("network") == "raw":
        node["network"] = "tcp"

    # 强制注入 skip-cert-verify，防止 Shadowrocket 因自签证书或免流伪装证书阻断连接
    if node.get("tls") or ntype in ("vless", "vmess", "trojan", "hysteria2", "tuic"):
        node["skip-cert-verify"] = True

    # 补全 servername / sni
    if node.get("tls") and not node.get("servername") and not node.get("sni"):
        if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", server):
            node["servername"] = server

    # 清理 Shadowrocket 不兼容的专属高级参数
    for incompatible in ("smux", "tfo", "mptcp"):
        node.pop(incompatible, None)

    if ntype == "vless" and node.get("reality-opts"):
        ropts = node["reality-opts"]
        if not isinstance(ropts, dict) or not ropts.get("public-key"):
            return None

    if node.get("network") == "grpc":
        gopts = node.get("grpc-opts")
        if not isinstance(gopts, dict) or not gopts.get("grpc-service-name"):
            node["grpc-opts"] = {"grpc-service-name": "grpc"}

    if node.get("network") == "ws":
        wopts = node.get("ws-opts")
        if wopts is not None and not isinstance(wopts, dict):
            node["ws-opts"] = {"path": "/"}
        elif isinstance(wopts, dict) and "path" in wopts:
            # 标准化解码 WebSocket 路径（例如将 %2F 转换为 /），避免客户端解析 404
            node["ws-opts"]["path"] = urllib.parse.unquote(str(wopts["path"]))

    return node


# ==========================================
# 3. 异步拉取与两阶段测速核心逻辑
# ==========================================

async def fetch_yaml(session: aiohttp.ClientSession, sem: asyncio.Semaphore, url: str) -> List[Dict[str, Any]]:
    """支持 GitHub 镜像自动回退的 YAML 节点拉取器"""
    parts = url.split("/")
    repo_label = f"{parts[3]}/{parts[4]}/{parts[-1]}" if len(parts) > 5 else url

    async with sem:
        for prefix in MIRROR_PREFIXES:
            target_url = f"{prefix}{url}" if prefix else url
            try:
                async with session.get(target_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text(errors="ignore")
                    clean_text = CONTROL_CHAR_RE.sub('', text)
                    data = yaml.safe_load(clean_text)
                    if isinstance(data, dict):
                        proxies = data.get("proxies") or data.get("Proxy")
                        if isinstance(proxies, list) and len(proxies) > 0:
                            mirror_tag = "直连" if not prefix else "镜像"
                            print(f"[+] ({mirror_tag}) 获取 {len(proxies):4d} 个节点 <- {repo_label}")
                            return proxies
            except Exception:
                continue

    return []


async def tcp_fast_precheck(sem: asyncio.Semaphore, node: Dict[str, Any]) -> tuple[Dict[str, Any], float] | None:
    """第一阶段：高并发 TCP 握手预筛，剔除死节点"""
    async with sem:
        start = time.perf_counter()
        try:
            conn = asyncio.open_connection(str(node["server"]), int(node["port"]))
            _, writer = await asyncio.wait_for(conn, timeout=1.5)
            lat = (time.perf_counter() - start) * 1000
            writer.close()
            await writer.wait_closed()
            return node, lat
        except Exception:
            return None


async def test_single_node_via_api(
    session: aiohttp.ClientSession, sem: asyncio.Semaphore, node: Dict[str, Any], rounds: int = 2
) -> tuple[Dict[str, Any], float] | None:
    """第二阶段：调用本地 mihomo API 进行多轮真实 HTTP 204 代理握手测速"""
    encoded_name = urllib.parse.quote(node["name"], safe="")
    api_url = f"http://127.0.0.1:{API_PORT}/proxies/{encoded_name}/delay"
    params = {"url": TEST_URL, "timeout": TEST_TIMEOUT_MS}
    delays = []

    async with sem:
        for i in range(rounds):
            try:
                async with session.get(
                    api_url, params=params, timeout=aiohttp.ClientTimeout(total=3.0)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        delay = data.get("delay", 0)
                        if delay > 0:
                            delays.append(delay)
            except Exception:
                pass

            if i < rounds - 1:
                await asyncio.sleep(0.15)

    # 严格把关：必须 2 轮全部 100% 成功，0 容忍丢包（单轮偶尔通一轮的直接剔除）
    if len(delays) < rounds:
        return None

    avg_delay = sum(delays) / len(delays)
    jitter = max(delays) - min(delays)
    score = avg_delay + (jitter * 0.4)
    return node, score, avg_delay


async def run_mihomo_real_test(
    mihomo_bin: str, candidates: List[Dict[str, Any]]
) -> List[tuple[Dict[str, Any], float, float]]:
    """启动临时无头 mihomo 进程，并执行批量真连接测速"""
    for idx, node in enumerate(candidates):
        node["name"] = f"CANDIDATE_{idx}"

    phys_if = get_physical_interface()
    temp_config = {
        "mixed-port": 0,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "unified-delay": True,
        "external-controller": f"127.0.0.1:{API_PORT}",
        "dns": {
            "enable": True,
            "ipv6": False,
            "nameserver": ["223.5.5.5", "119.29.29.29"],
        },
        "proxies": candidates,
    }
    if phys_if:
        temp_config["interface-name"] = phys_if

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(temp_config, f, allow_unicode=True, sort_keys=False)

        print(f"[*] 正在启动本地 mihomo 内核进行真连接测速: {mihomo_bin}")
        proc = await asyncio.create_subprocess_exec(
            mihomo_bin, "-d", tmpdir, "-f", cfg_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            api_ready = False
            async with aiohttp.ClientSession() as session:
                for _ in range(30):
                    try:
                        async with session.get(f"http://127.0.0.1:{API_PORT}/version") as r:
                            if r.status == 200:
                                ver = await r.json()
                                print(f"[+] Mihomo 内核已就绪 (版本: {ver.get('version', 'unknown')})，开始 2 轮真连接 HTTP 204 握手测试...")
                                api_ready = True
                                break
                    except Exception:
                        await asyncio.sleep(0.2)

                if not api_ready:
                    out, err = await proc.communicate()
                    print(f"[-] Mihomo 启动失败！错误输出:\n{out.decode(errors='ignore')}\n{err.decode(errors='ignore')}")
                    return []

                sem = asyncio.Semaphore(45)
                tasks = [test_single_node_via_api(session, sem, node, rounds=2) for node in candidates]
                results = await asyncio.gather(*tasks)

            valid = [r for r in results if r is not None]
            valid.sort(key=lambda x: x[1])  # 按综合评分排序
            return valid

        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()


# ==========================================
# 4. 配置文件生成与主流程
# ==========================================

class NoAliasDumper(yaml.SafeDumper):
    """彻底禁用 YAML 锚点别名（&id001/*id001），保证小火箭等手机客户端完美兼容解析"""
    def ignore_aliases(self, data):
        return True


def proxy_to_uri(node: Dict[str, Any]) -> str | None:
    """将代理节点转换为 Shadowrocket / V2Ray 原生标准 URI 链接"""
    ntype = node.get("type", "").lower()
    name = urllib.parse.quote(node.get("name", "node"))
    server = node.get("server")
    port = node.get("port")

    if not server or not port:
        return None

    if ntype == "vless":
        uuid = node.get("uuid")
        tls = "tls" if node.get("tls") else "none"
        sni = node.get("servername") or node.get("sni") or ""
        net = node.get("network", "tcp")
        params = {"encryption": "none", "security": tls}
        if sni:
            params["sni"] = sni
        if net == "ws":
            params["type"] = "ws"
            wopts = node.get("ws-opts", {})
            if "path" in wopts:
                params["path"] = wopts["path"]
            headers = wopts.get("headers", {})
            if "Host" in headers:
                params["host"] = headers["Host"]
        elif net == "grpc":
            params["type"] = "grpc"
            gopts = node.get("grpc-opts", {})
            if "grpc-service-name" in gopts:
                params["serviceName"] = gopts["grpc-service-name"]
        if node.get("client-fingerprint"):
            params["fp"] = node["client-fingerprint"]
        qs = urllib.parse.urlencode(params)
        return f"vless://{uuid}@{server}:{port}?{qs}#{name}"

    elif ntype == "trojan":
        pwd = node.get("password")
        sni = node.get("sni") or node.get("servername") or ""
        net = node.get("network", "tcp")
        params = {"security": "tls"}
        if sni:
            params["sni"] = sni
        if net == "ws":
            params["type"] = "ws"
            wopts = node.get("ws-opts", {})
            if "path" in wopts:
                params["path"] = wopts["path"]
            headers = wopts.get("headers", {})
            if "Host" in headers:
                params["host"] = headers["Host"]
        qs = urllib.parse.urlencode(params)
        return f"trojan://{pwd}@{server}:{port}?{qs}#{name}"

    elif ntype == "vmess":
        vj = {
            "v": "2",
            "ps": node.get("name", ""),
            "add": server,
            "port": port,
            "id": node.get("uuid"),
            "aid": node.get("alterId", 0),
            "scy": node.get("cipher", "auto"),
            "net": node.get("network", "tcp"),
            "type": "none",
            "host": "",
            "path": "",
            "tls": "tls" if node.get("tls") else "",
        }
        if vj["net"] == "ws":
            wopts = node.get("ws-opts", {})
            if "path" in wopts:
                vj["path"] = wopts["path"]
            headers = wopts.get("headers", {})
            if "Host" in headers:
                vj["host"] = headers["Host"]
        import base64
        b64 = base64.b64encode(json.dumps(vj).encode()).decode()
        return f"vmess://{b64}"

    elif ntype == "hysteria2":
        pwd = node.get("password") or node.get("auth-str") or node.get("auth")
        sni = node.get("sni") or ""
        params = {"insecure": "1"}
        if sni:
            params["sni"] = sni
        qs = urllib.parse.urlencode(params)
        return f"hysteria2://{pwd}@{server}:{port}?{qs}#{name}"

    return None


def build_clash_meta_config(proxies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构建最终导入 ClashX Meta 的完整配置文件"""
    proxy_names = [p["name"] for p in proxies]
    return {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": [
                "https://doh.pub/dns-query",
                "https://dns.alidns.com/dns-query",
            ],
            "fallback": [
                "https://1.1.1.1/dns-query",
                "https://8.8.8.8/dns-query",
            ],
        },
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "🚀 节点选择",
                "type": "select",
                "proxies": ["⚡ 自动最快", "🛡️ 故障转移"] + proxy_names + ["DIRECT"],
            },
            {
                "name": "⚡ 自动最快",
                "type": "url-test",
                "proxies": proxy_names,
                "url": TEST_URL,
                "interval": 300,
                "tolerance": 50,
            },
            {
                "name": "🛡️ 故障转移",
                "type": "fallback",
                "proxies": proxy_names,
                "url": TEST_URL,
                "interval": 300,
            },
        ],
        "rules": [
            "GEOIP,LAN,DIRECT,no-resolve",
            "GEOIP,CN,DIRECT",
            "MATCH,🚀 节点选择",
        ],
    }


async def main():
    mihomo_bin = find_mihomo_binary()
    all_sources = get_all_sources()

    print(f"[*] 准备并发拉取 {len(all_sources)} 个 GitHub 节点源...")
    headers = {"User-Agent": "clash.meta/v1.19.0"}
    fetch_sem = asyncio.Semaphore(8)  # 限制同时拉取的仓库数，防止网络拥塞
    async with aiohttp.ClientSession(headers=headers) as session:
        raw_lists = await asyncio.gather(*[fetch_yaml(session, fetch_sem, u) for u in all_sources])

    # 清洗、去重，并严格限制同一 /24 C段网段的节点数（杜绝蜜罐或同网段集群刷屏）
    unique_nodes: Dict[str, Dict[str, Any]] = {}
    subnet_count: Dict[str, int] = {}

    for plist in raw_lists:
        for raw_node in plist:
            cleaned = validate_and_clean_node(raw_node)
            if not cleaned:
                continue
            srv = str(cleaned["server"]).lower()
            c_sub = get_c_subnet(srv)
            key = f"{cleaned['type']}:{srv}:{cleaned['port']}"
            if key not in unique_nodes:
                if subnet_count.get(c_sub, 0) < MAX_PER_C_SUBNET:
                    unique_nodes[key] = cleaned
                    subnet_count[c_sub] = subnet_count.get(c_sub, 0) + 1

    all_nodes = list(unique_nodes.values())
    print(f"\n[*] 清洗与去重后共获得 {len(all_nodes)} 个独立合规节点，开始第一阶段 TCP 快速预筛...")

    if not all_nodes:
        print("[-] 未获取到任何有效节点，请检查网络。")
        return

    # 第一阶段：高并发 TCP 握手预筛，选出前 250 个低延迟节点
    tcp_sem = asyncio.Semaphore(300)
    tcp_res = await asyncio.gather(*[tcp_fast_precheck(tcp_sem, n) for n in all_nodes])
    tcp_passed = [r for r in tcp_res if r is not None]
    tcp_passed.sort(key=lambda x: x[1])
    pre_candidates = [r[0] for r in tcp_passed[:PRECHECK_LIMIT]]

    print(f"[+] TCP 预筛完成：{len(tcp_passed)} 个节点端口连通，选取前 {len(pre_candidates)} 名进入【Mihomo 真连接测速】...")
    if not pre_candidates:
        print("[-] 无节点通过 TCP 预筛。")
        return

    # 第二阶段：调用本地 mihomo 内核进行 2 轮真连接 HTTP 204 测速
    real_passed = await run_mihomo_real_test(mihomo_bin, pre_candidates)
    print(f"[+] 真连接测速完成：{len(real_passed)}/{len(pre_candidates)} 个节点成功通过 2 轮完整代理协议握手！")

    if not real_passed:
        print("[-] 没有节点通过真连接测试，请稍后重试。")
        return

    # 提取前 30 个最稳定的真活节点并规范化命名
    top_nodes = []
    for idx, (node, score, avg_delay) in enumerate(real_passed[:FINAL_NODE_COUNT], 1):
        ntype = node["type"].upper()
        node["name"] = f"[{idx:02d}] {ntype} | 真连接 {avg_delay:.0f}ms"
        top_nodes.append(node)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILENAME)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(
            build_clash_meta_config(top_nodes),
            f,
            Dumper=NoAliasDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    # 同时导出标准 Base64 订阅文件 (sub.txt 与 sub)，供 Shadowrocket / V2RayN 等客户端 100% 原生解析
    import base64
    uris = [proxy_to_uri(n) for n in top_nodes]
    valid_uris = [u for u in uris if u]
    raw_sub_text = "\n".join(valid_uris)
    b64_sub_text = base64.b64encode(raw_sub_text.encode("utf-8")).decode("utf-8")

    sub_txt_path = os.path.join(OUTPUT_DIR, "sub.txt")
    with open(sub_txt_path, "w", encoding="utf-8") as f:
        f.write(b64_sub_text)

    sub_path = os.path.join(OUTPUT_DIR, "sub")
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(b64_sub_text)

    print(f"\n[🎉] 成功生成配置文件: {output_path} (包含 {len(top_nodes)} 个稳定节点)")
    print(f"[🎉] 成功生成 Shadowrocket 专属 Base64 订阅: {sub_txt_path} ({len(valid_uris)} 个原生 URI)")
    print("前 10 个最佳节点预览：")
    for p in top_nodes[:10]:
        print(f"  - {p['name']} ({p['server']}:{p['port']})")

    # 兼容写入本地旧路径
    if os.path.exists(LOCAL_BACKUP_DIR):
        try:
            shutil.copy2(output_path, os.path.join(LOCAL_BACKUP_DIR, OUTPUT_FILENAME))
            shutil.copy2(sub_txt_path, os.path.join(LOCAL_BACKUP_DIR, "sub.txt"))
        except Exception:
            pass

    # 自动发布至 Vercel 全球公网
    publish_to_vercel(PROJECT_DIR, OUTPUT_FILENAME)


def publish_to_vercel(project_dir: str, file_name: str):
    """自动将 public 目录部署发布至 Vercel 边缘网络（CI 环境由 Git 推送触发自动部署）"""
    primary_url = f"https://freekuaile.dpdns.org/{file_name}"
    backup_url = f"https://public-zeta-lime.vercel.app/{file_name}"

    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
        print("\n" + "=" * 65)
        print("[⚡] 检测到处于云端 GitHub Actions 环境，文件已更新至 public 目录，将由 Git 自动提交！")
        print(f"   主订阅链接: {primary_url}")
        print("=" * 65)
        return

    print("\n" + "=" * 65)
    print("🚀 正在自动同步并发布至 Vercel 全球边缘网络...")
    try:
        proc = subprocess.run(
            ["vercel", "deploy", "public", "--prod", "--yes"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            print("[✅] Vercel 发布成功！全球 CDN 公网订阅已就绪！")
        else:
            print(f"[!] Vercel 返回信息: {proc.stdout.strip() or proc.stderr.strip()}")
    except Exception as e:
        print(f"[-] 自动发布 Vercel 出现异常: {e}")

    print("=" * 65)
    print("📲 Shadowrocket (小火箭) 全球公网订阅链接：")
    print(f"   主订阅链接 (自定义域名): {primary_url}")
    print(f"   备用订阅链接 (Vercel):  {backup_url}")
    print("=" * 65)
    print("小火箭配置指引:")
    print("   1. 打开手机 Shadowrocket，点击右上角「+」")
    print("   2. 类型 (Type) 选择「Subscribe」")
    print(f"   3. URL 填入: {primary_url}")
    print("   4. 保存即可！无论手机在 5G 还是任何地方，随时随地一键更新！")
    print("-" * 65)


if __name__ == "__main__":
    asyncio.run(main())