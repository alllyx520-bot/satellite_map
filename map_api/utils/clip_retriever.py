"""RemoteCLIP 真·遥感跨模态检索 —— 给"切块相关性筛选"用。

替代旧的颜色直方图启发式(假 CLIP):用遥感专用 CLIP(RemoteCLIP, ViT-B-32)
做 文本→图块 的语义相似度排序,召回与问题最相关的分块。

设计要点:
- 懒加载单例:模型只在首次调用时载入并常驻显存,避免每次请求重载。
- 优雅降级:torch / open_clip / 权重任一不可用时,所有函数安全返回 None,
  调用方(smart_query_analyzer.rank_tiles)自动回退到颜色启发式 —— 没装 torch
  的机器(如答辩备用机)照常能跑。
- 权重默认在项目 models/RemoteCLIP-ViT-B-32.pt(用 scripts/download_remoteclip.py 下一次),
  可用环境变量 REMOTECLIP_CKPT 覆盖。

权重来源:HuggingFace `chendelong/RemoteCLIP`。
"""
import os

# 权重路径:默认 <项目根>/models/RemoteCLIP-ViT-B-32.pt
_DEFAULT_CKPT = os.path.join(os.path.dirname(__file__), '..', '..', 'models', 'RemoteCLIP-ViT-B-32.pt')
CKPT_PATH = os.environ.get('REMOTECLIP_CKPT', _DEFAULT_CKPT)
MODEL_ARCH = 'ViT-B-32'

# 懒加载缓存:None=未尝试,False=尝试过但不可用,dict=已加载
_state = None


def _load():
    """懒加载 RemoteCLIP。成功返回 state dict,失败返回 None(并缓存结果)。"""
    global _state
    if _state is not None:
        return _state or None
    try:
        import torch
        import open_clip
        if not os.path.exists(CKPT_PATH):
            print(f"[RemoteCLIP] 权重不存在: {CKPT_PATH} —— 回退颜色启发式。先跑 scripts/download_remoteclip.py")
            _state = False
            return None
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        model, _, preprocess = open_clip.create_model_and_transforms(MODEL_ARCH)
        ckpt = torch.load(CKPT_PATH, map_location='cpu')
        model.load_state_dict(ckpt)
        model = model.to(device).eval()
        tokenizer = open_clip.get_tokenizer(MODEL_ARCH)
        _state = {"torch": torch, "model": model, "preprocess": preprocess,
                  "tokenizer": tokenizer, "device": device}
        print(f"[RemoteCLIP] 已加载 ({device})")
        return _state
    except Exception as e:
        print(f"[RemoteCLIP] 加载失败,回退颜色启发式: {e}")
        _state = False
        return None


def available():
    return _load() is not None


def score_tiles(tile_paths, text_query):
    """对每个 tile 算与 text_query(英文遥感语义短语)的余弦相似度。
    返回 [(path, score), ...];模型不可用或出错时返回 None。"""
    st = _load()
    if not st or not tile_paths or not text_query:
        return None
    torch = st["torch"]
    try:
        from PIL import Image
        with torch.no_grad():
            text = st["tokenizer"]([text_query]).to(st["device"])
            tfeat = st["model"].encode_text(text)
            tfeat = tfeat / tfeat.norm(dim=-1, keepdim=True)

            imgs = torch.stack([st["preprocess"](Image.open(p).convert('RGB')) for p in tile_paths]).to(st["device"])
            ifeat = st["model"].encode_image(imgs)
            ifeat = ifeat / ifeat.norm(dim=-1, keepdim=True)

            sims = (ifeat @ tfeat.T).squeeze(-1)   # [N]
        scores = sims.detach().cpu().tolist()
        if not isinstance(scores, list):
            scores = [scores]
        return list(zip(tile_paths, scores))
    except Exception as e:
        print(f"[RemoteCLIP] 评分失败,回退: {e}")
        return None
