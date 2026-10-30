# 数据接入契约

最终主表是 `data/source/video_dataset.json`，以 `videoID` 为稳定主键。原始素材根目录通过 `asset_root` 配置，不复制到仓库。每个 BV 目录包含 `metadata.json`、`asr_raw.json`、`ocr_raw.json` 和抽帧；同级 MP4 是视频文件。

小文件的历史中文名与当前英文名见 `data/source/rename_map.json`。外置素材目录保留原来的 BV 命名，避免复制和改名带来的大文件风险。

运行时将主表映射成英文对象：`VideoInput`、`Evidence` 和带定位信息的 ASR/OCR segment。`description`、`caption` 在当前数据中是模型派生且未经人工核验，不能直接作为人工金标准。ASR 与 OCR 的原始结果保留，清洗只做空白规范化、重复标记和模式筛选，不做无证据纠错。

OCR 模式：

- `off`：不把 OCR 片段传入模型，但仍保留素材路径。
- `raw`：传入全部非空原始 OCR 片段及秒数。
- `filtered`：去除完全重复和单字符噪声，默认模式。

每次运行保存素材路径和 metadata/ASR/OCR 的 SHA-256，保证任务可以追溯到固定输入版本。评测标准、人工标签和正式 Memory 通过独立版本接入，不覆盖原始数据。
