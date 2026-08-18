#!/usr/bin/env python3
"""把 esp-sr 的 wakenet 模型打成设备可下载的 assets 包。

用法:
  python scripts/pack_wake_word_assets.py \
      --esp-sr <固件仓库>/managed_components/espressif__esp-sr \
      --model wn9_himiaomiao_tts \
      --out data/bin/assets-himiaomiao.bin

产物格式与 esp_mmap_assets 的 spiffs_assets_gen.py::pack_assets 一致：
  header = u32 文件数 | u32 校验和 | u32 (表+数据) 长度
  表项    = name[32] | u32 size | u32 offset | u16 width | u16 height
  数据    = 每个文件前置 b'ZZ' 魔数，表里的 offset 指向魔数本身
  校验和  = sum(表+数据) & 0xFFFF （无符号）
  排序    = (扩展名, 主文件名)
已用官方包 wn9_nihaoxiaozhi_tts-none-none.bin 做过逐字节回归。
"""
import argparse, os, shutil, struct, subprocess, sys, tempfile

NAME_LEN = 32


def pack(files, out_path):
    ordered = sorted(files, key=lambda kv: os.path.splitext(kv[0])[::-1])
    merged, infos = bytearray(), []
    for name, path in ordered:
        data = open(path, "rb").read()
        if len(name.encode()) > NAME_LEN:
            raise SystemExit(f"文件名超过 {NAME_LEN} 字节: {name}")
        infos.append((name, len(merged), len(data)))
        merged += b"\x5a\x5a" + data

    table = bytearray()
    for name, offset, size in infos:
        table += name.encode().ljust(NAME_LEN, b"\0")[:NAME_LEN]
        table += struct.pack("<IIHH", size, offset, 0, 0)

    combined = bytes(table) + bytes(merged)
    header = struct.pack("<III", len(infos), sum(combined) & 0xFFFF, len(combined))
    with open(out_path, "wb") as f:
        f.write(header + combined)
    return infos


def build_srmodels(esp_sr: str, model: str, workdir: str) -> str:
    """用 esp-sr 自带的 movemodel.py 生成只含指定模型的 srmodels.bin。"""
    sdkconfig = os.path.join(workdir, "sdkconfig.mini")
    with open(sdkconfig, "w") as f:
        f.write(f"CONFIG_SR_WN_{model.upper()}=y\n")
        for none in ("SR_MN_CN_NONE", "SR_MN_EN_NONE", "SR_NSN_NONE", "SR_VADN_NONE"):
            f.write(f"CONFIG_{none}=y\n")
    build = os.path.join(workdir, "build")
    os.makedirs(build, exist_ok=True)
    subprocess.run(
        [sys.executable, os.path.join(esp_sr, "model", "movemodel.py"),
         "-d1", sdkconfig, "-d2", esp_sr, "-d3", build],
        check=True, capture_output=True,
    )
    out = os.path.join(build, "srmodels", "srmodels.bin")
    if not os.path.isfile(out):
        raise SystemExit(f"movemodel.py 未生成 srmodels.bin，请检查模型名 {model}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--esp-sr", required=True, help="esp-sr 组件目录")
    ap.add_argument("--model", required=True, help="wakenet 模型名，如 wn9_himiaomiao_tts")
    ap.add_argument("--out", required=True, help="输出的 assets 包路径")
    args = ap.parse_args()

    model_dir = os.path.join(args.esp_sr, "model", "wakenet_model", args.model)
    if not os.path.isdir(model_dir):
        raise SystemExit(f"模型不存在: {model_dir}")

    with tempfile.TemporaryDirectory() as tmp:
        srmodels = build_srmodels(args.esp_sr, args.model, tmp)
        staged = os.path.join(tmp, "stage")
        os.makedirs(staged, exist_ok=True)
        shutil.copy(srmodels, os.path.join(staged, "srmodels.bin"))
        with open(os.path.join(staged, "index.json"), "w") as f:
            f.write('{\n    "version": 1,\n    "srmodels": "srmodels.bin"\n}')

        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        infos = pack(
            [(n, os.path.join(staged, n)) for n in ("srmodels.bin", "index.json")],
            args.out,
        )

    print(f"{args.out}  ({os.path.getsize(args.out)} bytes)  model={args.model}")
    for n, o, s in infos:
        print(f"  {n:16} offset={o:<9} size={s}")


if __name__ == "__main__":
    main()
