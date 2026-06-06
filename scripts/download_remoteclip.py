"""一次性下载 RemoteCLIP ViT-B-32 权重到项目 models/ 目录。

用法(在 satellite_env 里):
    E:\\Anaconda\\envs\\satellite_env\\python.exe scripts\\download_remoteclip.py

下完后 map_api/utils/clip_retriever.py 会自动加载 models/RemoteCLIP-ViT-B-32.pt。

实现:直接用 requests 流式下载 HuggingFace 的文件 URL,并 proxies={http:None}
绕过系统 VPN(与项目其它下载一致)。不走 huggingface_hub 客户端 —— 其 1.x 版
的 httpx 实现有 "client has been closed" 的坑。若官方源慢,可改用环境变量
HF_BASE 指向镜像(如 https://hf-mirror.com)。
"""
import os
import sys

HF_BASE = os.environ.get('HF_BASE', 'https://huggingface.co')
URL = f"{HF_BASE}/chendelong/RemoteCLIP/resolve/main/RemoteCLIP-ViT-B-32.pt"
DEST_DIR = os.path.join(os.path.dirname(__file__), '..', 'models')
DEST = os.path.join(DEST_DIR, 'RemoteCLIP-ViT-B-32.pt')


def main():
    import requests
    os.makedirs(DEST_DIR, exist_ok=True)
    if os.path.exists(DEST) and os.path.getsize(DEST) > 1_000_000:
        print(f"已存在,跳过: {DEST}")
        return
    print(f"下载 {URL} ...")
    proxies = {"http": None, "https": None}   # 绕过系统 VPN
    with requests.get(URL, stream=True, timeout=60, proxies=proxies) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0))
        done = 0
        tmp = DEST + '.part'
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    sys.stdout.write(f"\r  {done >> 20}/{total >> 20} MB ({pct}%)")
                    sys.stdout.flush()
        os.replace(tmp, DEST)
    print(f"\n✅ 已保存到 {DEST}")


if __name__ == '__main__':
    main()
