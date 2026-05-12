# LifelogProj

LifelogProj 用于把生活视频处理成可供回忆训练或 Unity 互动内容使用的结构化数据。当前项目包含两条主要使用方式：

- 命令行流水线：输入视频，输出 `extracted_context.json`、`GameMeta.json`、`GameFlow.json`
- Flask API 服务：对外提供上下文、游戏配置、TTS 和静态资源访问接口

## 项目目标

项目的核心流程是：

1. 解析生活视频，提取活动与子事件
2. 为事件抽取场景线索、人物线索、物体线索
3. 抽取关键参考帧
4. 可选生成 AIGC 增强图和游戏选项图
5. 输出 Unity 可直接消费的游戏数据

## 当前目录结构

```text
LifelogProj/
├─ api_server.py                 # Flask API 服务
├─ run_pipeline.py               # 主流水线入口
├─ make_context.py               # 视频理解、抽帧、AIGC 增强
├─ bailian_video_processor.py    # 百炼视频理解适配层
├─ generate_unity_json.py        # extracted_context -> GameMeta/GameFlow
├─ clue_aigc_generator.py        # 火山引擎图像生成
├─ llm_client.py                 # SiliconFlow LLM 封装
├─ db.py                         # SQLite 记录与状态管理
├─ my_basics.py                  # 基础数据结构与 JSON 解析工具
├─ strategy.py                   # 独立的回忆策略模块
├─ tencent_cos_uploader.py       # 腾讯 COS 上传工具
├─ twelvelabs_processor.py       # 历史/备用处理器
├─ prompts/                      # 提示词模板与策略配置
├─ examples/                     # 示例 JSON 和素材
├─ videos/                       # 待处理视频
├─ output/                       # 处理结果、日志、图片、TTS 缓存
├─ requirements.txt
├─ .env
└─ lifelog.db
```

## 环境要求

- Python `3.12`
- 建议先创建虚拟环境

安装依赖：

```bash
pip install -r requirements.txt
```

说明：

- `requirements.txt` 覆盖了主流水线依赖
- `api_server.py` 的 `/api/tts` 依赖 `edge-tts`，当前未写入 `requirements.txt`，如果需要启用 TTS，需要额外安装：

```bash
pip install edge-tts
```

## Docker 单入口部署

对 Unity 的局域网访问，不需要域名。推荐只暴露一个入口端口，由 `nginx` 提供前端并反向代理 `/api` 与 `/assets` 到后端。

启动前，在项目根目录设置对外访问地址：

```bash
# Windows PowerShell
$env:PUBLIC_BASE_URL="http://192.168.1.23"
docker compose up -d --build
```

其中 `192.168.1.23` 替换成运行 Docker 那台电脑的局域网 IP。

这样：

- 浏览器访问前端：`http://192.168.1.23/`
- Unity 访问接口：`http://192.168.1.23/api/...`
- Unity 访问资源：`http://192.168.1.23/assets/...`

后端会使用 `PUBLIC_BASE_URL` 生成返回给 Unity 的 `game_meta_url`、`game_flow_url` 和资源 URL，避免返回 `localhost` 或容器内部地址。

## 环境变量

项目通过 `.env` 或系统环境变量读取配置。常用变量如下。

### 百炼视频理解

- `BAILIAN_API_KEY` 或 `DASHSCOPE_API_KEY`
- `BAILIAN_BASE_URL`
- `BAILIAN_MODEL`
- `BAILIAN_MAX_TOKENS`
- `BAILIAN_TEMPERATURE`
- `BAILIAN_INPUT_MODE`
- `BAILIAN_UPLOAD_API_URL`
- `BAILIAN_UPLOAD_TEMP`
- `BAILIAN_PARSE_RETRIES`

### 火山引擎 AIGC

- `VOLC_ACCESS_KEY`
- `VOLC_SECRET_KEY`

### SiliconFlow

- `SILICONFLOW_API_KEY`
- `SILICONFLOW_MODEL`
- `SILICONFLOW_LOG_PATH`

### 数据与服务

- `PIPELINE_DB_PATH`
- `API_BASE_URL`
- `PORT`

### 可选工具链

- `FFMPEG_BIN`
- `FFPROBE_BIN`

## 命令行流水线

主入口：

```bash
python run_pipeline.py --user 001 --video 001.mp4
```

常用参数：

- `--user`：输出子目录名，同时也是数据库中的用户标识
- `--video`：视频文件名，代码会从 `videos/` 下读取
- `--activity`：活动名称，默认 `逛超市`
- `--people`：参与人，默认 `我`
- `--time`：活动时间
- `--location`：活动地点
- `--output-root`：输出根目录，默认 `output`
- `--base-url`：资源外链根地址，可选
- `--force-regenerate-images`：清空已有图片路径并重新抽帧/重生成
- `--compress-video`：先压缩视频再处理
- `--compress-width`：压缩宽度，默认 `960`
- `--compress-height`：压缩高度，默认 `540`
- `--one-shot / --no-one-shot`：参数仍保留，但当前主流程实际固定走单次完整抽取

补充说明：

- 如果视频 metadata 中存在拍摄时间和 GPS 信息，`run_pipeline.py` 会优先自动提取
- 如果未提供拍摄时间且 metadata 中没有时间，会回退到视频文件的修改时间
- 如果设置了 `VOLC_ACCESS_KEY` 和 `VOLC_SECRET_KEY`，流水线会继续生成增强图
- 流水线会把本次控制台输出同步写入 `output/<user>/logs/run_*.log`

## 输出目录

单次处理后，默认会在 `output/<user>/` 下生成：

```text
output/<user>/
├─ extracted_context.json
├─ GameMeta.json
├─ GameFlow.json
├─ logs/
├─ frames/
├─ enhanced/
├─ option_images/
├─ tts/
└─ compressed/
```

其中：

- `extracted_context.json`：视频解析后的完整上下文
- `GameMeta.json`：Unity 游戏元信息
- `GameFlow.json`：Unity 游戏流程定义
- `frames/`：从原视频抽取的参考帧
- `enhanced/`：AIGC 增强图
- `option_images/`：回忆题选项图
- `tts/`：API 生成的语音缓存
- `compressed/`：压缩后的视频

## API 服务

启动方式：

```bash
python api_server.py
```

默认监听：

```text
http://0.0.0.0:8000
```

当前接口：

- `GET /api/health`
  - 健康检查
- `GET /api/context?user=<user>`
  - 返回 `output/<user>/extracted_context.json`
- `GET /api/game-meta?user=<user>&refresh=1`
  - 返回 `GameMeta.json`
  - 当 `refresh=1` 时会根据 `extracted_context.json` 重新生成
- `GET /api/game-flow?user=<user>&refresh=1`
  - 返回 `GameFlow.json`
  - 当 `refresh=1` 时会根据 `extracted_context.json` 重新生成
- `POST /api/tts`
  - 输入文本生成语音并缓存到 `output/<user>/tts/`
- `GET /assets/<user>/<path>`
  - 访问输出目录下的图片、音频等静态资源

`/api/tts` 请求体示例：

```json
{
  "user": "001",
  "text": "今天我们先回忆一下逛超市的过程。",
  "voice": "zh-CN-XiaoxiaoNeural",
  "rate": "+0%",
  "volume": "+0%",
  "format": "mp3"
}
```

## 主要模块说明

### run_pipeline.py

项目的主入口，负责串起整条处理链：

- 初始化日志
- 初始化 SQLite
- 读取视频与数据库记录
- 必要时压缩视频
- 构建 `ActivityContext`
- 调用百炼完成完整上下文抽取
- 抽取参考帧
- 可选执行 AIGC 增强
- 生成 Unity JSON
- 更新数据库状态

### make_context.py

负责中间上下文处理，当前包含三类核心能力：

- `execute_video_processor()`：事件切分 + 每事件实体提取，支持 checkpoint 恢复
- `execute_video_processor_single_call()`：一次性生成完整 `extracted_context`
- `extract_reference_frames()`：根据时间戳抽帧
- `execute_scene_clue_enhancement()`：对场景线索补图
- `execute_entity_clue_enhancement()`：对人物/物体线索补图

当前 `run_pipeline.py` 默认使用的是 `execute_video_processor_single_call()`。

### bailian_video_processor.py

阿里云百炼 OpenAI 兼容接口适配层。

主要能力：

- 本地视频临时上传至百炼 OSS
- 基于提示词调用模型
- 输出原始响应到 `output/<user>/bailian/`
- 支持：
  - `analyze_stage1_video_parse()`
  - `analyze_stage2_event_rebuild()`
  - `analyze_full_context()（旧兼容链路）`

### generate_unity_json.py

负责把 `extracted_context.json` 转换成 Unity 使用的：

- `GameMeta.json`
- `GameFlow.json`

该模块还会：

- 去重事件与实体
- 生成活动选择、事件回忆、排序、细节回忆等 stage
- 基于 `SentenceTransformer` 做相似度处理
- 通过 `SiliconFlowLLM` 生成干扰项
- 可选通过 `VolcEngineAIGCGenerator` 生成选项图

首次运行时会下载 `moka-ai/m3e-base` 模型到本地缓存目录。

### clue_aigc_generator.py

火山引擎图像生成封装，用于：

- 图生图增强参考帧
- 文生图生成选项图片

### llm_client.py

SiliconFlow 的轻量封装，主要包含：

- `chat()`
- `translate_text()`
- `translate_json_values()`

默认会把响应记录到 `output/siliconflow_responses.txt`。

### db.py

负责 SQLite 管理，当前默认数据库文件为项目根目录下的 `lifelog.db`。

主要表：

- `user_videos`
- `user_pipeline`，保留兼容旧结构

主要能力：

- 初始化表结构
- 按 `user_id + video_name` 维护视频处理记录
- 保存 `extracted_context_path`、`gameflow_path`、`gamemeta_path`
- 保存 `pipeline_state`

### api_server.py

提供对外接口，不直接执行视频处理，但会在访问 `game-meta` 或 `game-flow` 时按需从已有 `extracted_context.json` 重新生成目标 JSON。

### strategy.py

这是一个相对独立的辅助模块，用于“回忆策略”生成，不属于当前主流水线必经环节。

## 关键数据文件

### extracted_context.json

通常包含以下字段：

- `activity`
- `people`
- `time`
- `location`
- `activity_description`
- `activity_visual_clue`
- `events`

`events` 下每个事件通常包含：

- `name`
- `description`
- `start_time`
- `end_time`
- `scene_clues`
- `key_persons`
- `key_objects`
- `entities`

不同版本数据结构可能同时存在 `entities` 与按类别拆分后的字段，生成阶段会尽量兼容处理。

### GameMeta.json

主要包含游戏级别、叙事与素材映射信息。

### GameFlow.json

主要包含按 stage 组织的回忆流程，常见字段包括：

- `stage_id`
- `stage_name`
- `stage_type`
- `task_type`
- `description`
- `correct_answer`
- `options`
- `reference_frame_path`
- `enhanced_image_path`
- `related_narrative`
- `advanced_recall_questions`

## prompts 与 examples

- `prompts/` 保存百炼调用和策略生成相关模板
- `examples/` 保存示例输入输出，可用于调试结构字段

## 当前实现状态

- 主流水线已切换到百炼处理器，`twelvelabs_processor.py` 更偏历史遗留或备用模块
- `--one-shot` 参数仍在，但当前主入口没有根据该参数切换不同抽取分支
- API 服务依赖已有 `output/<user>/extracted_context.json`，不会替代完整视频处理流程
- TTS 功能是可选附加能力，不属于核心视频理解流程

## 最小使用流程

1. 把待处理视频放到 `videos/`
2. 配置 `.env` 中的百炼相关变量
3. 运行：

```bash
python run_pipeline.py --user 001 --video 001.mp4
```

4. 查看输出：

- `output/001/extracted_context.json`
- `output/001/GameMeta.json`
- `output/001/GameFlow.json`

5. 如果需要对外提供访问接口，再运行：

```bash
python api_server.py
```
