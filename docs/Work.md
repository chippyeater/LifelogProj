下面这版可以直接替换仓库根目录的 `readme.md`。

````markdown
# LifelogProj

LifelogProj 是一个实验用的生活记录回忆辅助系统后端项目。它将第一视角生活视频处理成结构化回忆材料，并生成 Unity 平板端可读取的回忆任务数据。

当前系统主要服务于以下实验闭环：

```text
医生网页端创建活动任务
→ 上传第一视角视频
→ Flask API 启动后端处理
→ run_pipeline.py 分析视频
→ 输出 extracted_context.json / GameMeta.json / GameFlow.json
→ Unity 平板端拉取任务数据
→ 老人完成回忆任务
→ Unity 保存回忆表现日志
→ 医生网页端查看处理结果与回忆表现
````

本项目当前重点不是正式部署，而是支持实验流程跑通。

---

## 1. 当前系统架构

```text
医生网页端
  │
  │ POST /api/tasks/process
  ▼
Flask API: api_server.py
  │
  │ subprocess 启动
  ▼
run_pipeline.py
  │
  ├─ 视频理解与上下文抽取
  ├─ 参考帧抽取
  ├─ AIGC 线索图生成（可选）
  ├─ Unity JSON 生成
  ▼
output/{user}/
  ├─ extracted_context.json
  ├─ GameMeta.json
  ├─ GameFlow.json
  ├─ frames/
  ├─ enhanced/
  ├─ option_images/
  └─ media/
       ├─ audio/
       └─ video/

Unity 平板端
  │
  ├─ GET /api/job-status?user={user}
  ├─ GET /api/game-meta?user={user}
  ├─ GET /api/game-flow?user={user}
  └─ GET /assets/{user}/{relativePath}
```

关键判断：

* 完整视频处理入口仍然是 `run_pipeline.py`
* 网页端通过 `POST /api/tasks/process` 包装 CLI 处理流程
* Unity 不直接跑视频处理，只读取后端生成好的 `GameMeta.json` 和 `GameFlow.json`
* 医生网页端的处理结果预览应主要读取 `/api/context`，即 `extracted_context.json`
* `GameMeta.json` / `GameFlow.json` 是 Unity 运行格式，不适合作为医生网页端的主要展示结构

---

## 2. 当前目录结构

```text
LifelogProj/
├─ readme.md
├─ api_server.py                 # Flask API 服务入口
├─ run_pipeline.py               # 视频处理主流水线入口
├─ make_context.py               # 视频理解、上下文构建、抽帧、线索增强
├─ bailian_video_processor.py    # 阿里云百炼视频理解适配层
├─ generate_unity_json.py        # extracted_context -> GameMeta/GameFlow
├─ clue_aigc_generator.py        # 火山引擎图像生成封装
├─ llm_client.py                 # SiliconFlow LLM 封装
├─ db.py                         # SQLite 状态记录
├─ my_basics.py                  # 基础数据结构与 JSON 工具
├─ strategy.py                   # 回忆引导策略模块，当前不属于主流水线必经路径
├─ tencent_cos_uploader.py       # 腾讯 COS 上传工具，当前非核心流程
├─ twelvelabs_processor.py       # 历史/备用处理器
├─ prompts/                      # Prompt 模板
├─ examples/                     # 示例数据
├─ videos/                       # 输入视频目录
├─ output/                       # 输出目录，不应提交 GitHub
├─ requirements.txt
├─ .env                          # 本地环境变量，不应提交 GitHub
└─ lifelog.db                    # 本地 SQLite 数据库，不应提交 GitHub
```

如果仓库中后续加入医生网页端，建议放在：

```text
frontend/
```

如果仓库中后续加入 Unity 工程，建议明确放在：

```text
unity/
```

当前后端不依赖前端项目存在。

---

## 3. 环境准备

### Python 版本

建议使用：

```text
Python 3.12
```

### 安装依赖

```bash
pip install -r requirements.txt
```

如果需要使用 `/api/tts`：

```bash
pip install edge-tts
```

### 必要工具

视频压缩、抽帧、音频/视频片段导出依赖：

```text
ffmpeg
ffprobe
```

可通过环境变量指定路径：

```text
FFMPEG_BIN
FFPROBE_BIN
```

---

## 4. 环境变量

项目通过 `.env` 或系统环境变量读取配置。

### 百炼视频理解

```text
BAILIAN_API_KEY
DASHSCOPE_API_KEY
BAILIAN_BASE_URL
BAILIAN_MODEL
BAILIAN_MAX_TOKENS
BAILIAN_TEMPERATURE
BAILIAN_INPUT_MODE
BAILIAN_UPLOAD_API_URL
BAILIAN_UPLOAD_TEMP
BAILIAN_PARSE_RETRIES
```

### 火山引擎 AIGC 图像生成

```text
VOLC_ACCESS_KEY
VOLC_SECRET_KEY
```

未配置时，不应依赖 AIGC 图增强功能。

### SiliconFlow

```text
SILICONFLOW_API_KEY
SILICONFLOW_MODEL
SILICONFLOW_LOG_PATH
```

主要用于干扰项生成、翻译等文本任务。

### API 服务

```text
PORT
API_BASE_URL
PIPELINE_DB_PATH
```

默认端口：

```text
8000
```

---

## 5. 最小手动运行流程

### 1. 放入视频

将视频放入：

```text
videos/
```

例如：

```text
videos/001.mp4
```

### 2. 运行完整 pipeline

```bash
python run_pipeline.py --user 001 --video 001.mp4 --pipeline-mode full_context
```

常用参数：

```bash
python run_pipeline.py \
  --user 001 \
  --video 001.mp4 \
  --pipeline-mode full_context \
  --activity 逛超市 \
  --people 我 \
  --time 2026年02月26日22:30 \
  --location 超市
```

当前第一版主要使用 `full_context` 模式。

`staged` / `pipeline_cache` 更适合开发调试，不建议先暴露给医生网页端。

---

## 6. 输出目录

一次处理完成后，默认输出：

```text
output/{user}/
├─ extracted_context.json
├─ GameMeta.json
├─ GameFlow.json
├─ status.json
├─ logs/
├─ frames/
├─ enhanced/
├─ option_images/
├─ media/
│  ├─ audio/
│  └─ video/
├─ compressed/
└─ tts/
```

核心文件：

| 文件                       | 用途                      |
| ------------------------ | ----------------------- |
| `extracted_context.json` | 视频理解后的活动、子事件、实体、线索、细节问题 |
| `GameMeta.json`          | Unity 游戏元信息             |
| `GameFlow.json`          | Unity 游戏流程              |
| `status.json`            | 网页端/Unity 可读的简化任务状态     |
| `frames/`                | 原视频参考帧                  |
| `enhanced/`              | AIGC 增强图                |
| `option_images/`         | 选项图                     |
| `media/audio/`           | 音频片段                    |
| `media/video/`           | 视频片段                    |
| `tts/`                   | TTS 缓存                  |

---

## 7. 启动 API 服务

```bash
python api_server.py
```

默认监听：

```text
http://127.0.0.1:8000
```

健康检查：

```http
GET http://127.0.0.1:8000/api/health
```

返回：

```json
{
  "status": "ok"
}
```

---

## 8. 医生网页端接口

### 8.1 上传视频并启动处理

```http
POST /api/tasks/process
```

请求格式：

```text
multipart/form-data
```

字段：

| 字段                | 类型     | 说明                     |
| ----------------- | ------ | ---------------------- |
| `video`           | File   | 第一视角视频                 |
| `user`            | string | 用户编号                   |
| `userName`        | string | 用户姓名，后端可以先忽略           |
| `activity`        | string | 活动主题，例如“逛超市”           |
| `people`          | string | 参与人物，例如“我”             |
| `time`            | string | 活动时间                   |
| `location`        | string | 活动地点                   |
| `shopping_list`   | string | JSON.stringify 后的购物清单  |
| `taskDescription` | string | 任务说明，后端可以先忽略           |
| `pipeline_mode`   | string | 当前建议固定为 `full_context` |

`shopping_list` 建议结构：

```json
[
  {
    "name": "牛奶",
    "quantity": 1,
    "unit": "盒"
  },
  {
    "name": "苹果",
    "quantity": 3,
    "unit": "个"
  }
]
```

注意：

* `shopping_list` 只作为任务上下文和实体筛选弱约束
* 不应直接作为正确答案来源
* 不应直接把清单数量当作细节题答案
* 数量题只有在视频中能观察或推断实际数量时才生成

返回示例：

```json
{
  "ok": true,
  "user": "001",
  "task_id": "task_001_xxx",
  "job_id": "task_001_xxx",
  "video": "001_task_001_xxx_upload.mp4",
  "status": "processing",
  "ready": false,
  "progress": 5,
  "current_step": "正在分析视频",
  "error": null
}
```

---

### 8.2 查询处理状态

```http
GET /api/job-status?user=001
```

返回示例：

```json
{
  "ok": true,
  "user": "001",
  "task_id": "task_001",
  "video": "001_task_001_upload.mp4",
  "status": "processing",
  "progress": 40,
  "current_step": "正在分析视频细节",
  "ready": false,
  "pipeline_state": {
    "events": "done",
    "entities": "pending",
    "frames": "pending",
    "aigc": "pending",
    "unity": "pending",
    "last_error": null
  },
  "error": null
}
```

前端只应强依赖：

```text
ready
status
progress
current_step
error
```

`pipeline_state` 可以用于调试，不建议页面逻辑强依赖。

---

### 8.3 获取医生网页预览数据

```http
GET /api/context?user=001
```

返回：

```text
output/001/extracted_context.json
```

这是医生网页端“处理结果预览”的主数据源。

真实结构示意：

```json
{
  "activity": "逛超市",
  "people": "我",
  "time": "2026年02月26日22:30",
  "location": "超市",
  "source_video": "videos/005.mp4",
  "activity_visual_clue": {
    "frame": "00:00:05.000",
    "description": "我们正在超市入口处挑选购物篮，准备开始购物",
    "reference_frame_path": "frames/逛超市.jpg"
  },
  "events": {
    "e1": {
      "id": "e1",
      "name": "进入超市并挑选商品",
      "description": "我们穿过超市入口闸门后，进入生鲜区，浏览并挑选了多种水果和蔬菜。",
      "start_time": "00:00:15.000",
      "end_time": "00:01:05.000",
      "scene_clues": [
        {
          "frame": "00:00:25.000",
          "description": "货架上摆放着整齐的草莓、西瓜等水果",
          "reference_frame_path": "frames/进入超市并挑选商品.jpg",
          "enhanced_image_path": "enhanced/进入超市并挑选商品.jpg"
        }
      ],
      "entities": [
        {
          "id": "o1",
          "item_name": "小番茄",
          "frame": "00:00:45.000",
          "entity_clues": {
            "visual": ["红色", "圆形", "透明塑料盒包装"],
            "semantic": ["水果", "食材"]
          },
          "detail_pair": [
            {
              "question": "我们买了几盒小番茄？",
              "correct_answer": "2盒",
              "options": ["1盒", "2盒"]
            }
          ],
          "reference_frame_path": "frames/小番茄.jpg",
          "enhanced_image_path": "enhanced/小番茄.jpg"
        }
      ]
    }
  }
}
```

前端读取方式：

```text
context.activity
context.people
context.time
context.location
Object.values(context.events)
event.name
event.description
event.start_time
event.end_time
event.scene_clues
event.entities
entity.item_name
entity.entity_clues.visual
entity.entity_clues.semantic
entity.detail_pair
```

不要使用旧结构：

```text
activity_theme
sub_events
key_objects
object_details
qa
```

---

### 8.4 获取 Unity 元信息

```http
GET /api/game-meta?user=001
```

返回：

```json
{
  "game_levels": []
}
```

注意：

* 没有 `success`
* 没有 `meta`

前端判断是否成功：

```ts
Boolean(gameMeta?.game_levels?.length)
```

---

### 8.5 获取 Unity 流程

```http
GET /api/game-flow?user=001
```

返回：

```json
{
  "game_flow": []
}
```

注意：

* 没有 `success`
* 没有 `flow`

前端判断是否成功：

```ts
Boolean(gameFlow?.game_flow?.length)
```

---

### 8.6 访问静态资源

```http
GET /assets/{user}/{relativePath}
```

示例：

```text
http://127.0.0.1:8000/assets/001/frames/逛超市.jpg
http://127.0.0.1:8000/assets/001/enhanced/进入超市并挑选商品.jpg
http://127.0.0.1:8000/assets/001/option_images/stage_001_01_unknown.jpg
http://127.0.0.1:8000/assets/001/media/audio/activity_theme_audio.mp3
http://127.0.0.1:8000/assets/001/media/video/event_e1_xxx.mp4
```

资源接口不带 `/api`。

如果前端配置是：

```ts
API_BASE_URL = "http://127.0.0.1:8000/api"
```

则资源 origin 应为：

```ts
"http://127.0.0.1:8000"
```

前端应统一封装：

```ts
function getApiOrigin() {
  return CONFIG.ASSET_BASE_URL || CONFIG.API_BASE_URL.replace(/\/api\/?$/, "");
}

function normalizeAssetPath(path?: string | null) {
  if (!path) return "";
  return path
    .replace(/\\/g, "/")
    .replace(/^\/+/, "")
    .replace(/^output\/[^/]+\//, "");
}

function buildAssetUrl(user: string, path?: string | null) {
  const normalized = normalizeAssetPath(path);
  if (!normalized) return "";
  if (/^https?:\/\//.test(normalized)) return normalized;
  return `${getApiOrigin()}/assets/${encodeURIComponent(user)}/${normalized}`;
}
```

不要这样写：

```tsx
<img src="frames/xxx.jpg" />
```

应写：

```tsx
<img src={buildAssetUrl(user, path)} />
```

---

## 9. Unity 对接逻辑

Unity 当前逻辑：

```text
1. 轮询 /api/job-status?user={user}
2. ready == true 后请求 /api/game-meta?user={user}
3. 请求 /api/game-flow?user={user}
4. 解析 JSON 中的资源相对路径
5. 请求 /assets/{user}/{relativePath}
6. 下载后保存到 PersistentDataPath
7. 进入回忆任务
```

因此：

* 不要随意修改 `GameMeta.json` / `GameFlow.json` 的结构
* 如果必须修改字段，必须同步改 Unity 解析代码
* 医生网页端不应该反向要求后端修改 Unity JSON 结构
* 医生网页预览应主要读 `extracted_context.json`

---

## 10. GameFlow 阶段结构

`GameFlow.json` 的根字段：

```json
{
  "game_flow": []
}
```

常见 stage 字段：

```text
stage_id
stage_name
stage_type
stage_index
task_type
task_description
correct_answer
options
hint_scene_images
hints
enhanced_image_path
reference_frame_path
long_description
distractor_option
video_clip_url
correct_position
total_items
related_narrative
advanced_recall_questions
```

常见任务类型：

```text
selection
recall
sequencing
detail_recall
```

常见阶段类型：

```text
structure
narrative
item
```

---

## 11. GameMeta 结构

`GameMeta.json` 的根字段：

```json
{
  "game_levels": []
}
```

常见字段：

```text
structure_id
structure_name
structure_index
people
time
location
activity_description
hint_scene_images
narratives
```

每个 narrative 常见字段：

```text
narrative_id
narrative_name
narrative_description
narrative_index
start_time
end_time
scene_clues
video_clip
items
```

每个 item 常见字段：

```text
item_id
item_name
item_type
item_index
frame
entity_clues
detail_pair
reference_frame_path
enhanced_image_path
```

---

## 12. 回忆报告与行为日志

Unity 会在任务过程中记录用户行为。当前已有的 `TaskContext.json` / action history 结构可以支持基础回忆报告。

当前可统计：

```text
每阶段错误次数
错误选项
提示请求次数
作答历史
完成阶段
当前阶段
```

当前典型 action 结构：

```json
{
  "action_id": "d041847c-43c7-4864-8815-b0d1ff449cf6",
  "timestamp": "2026-05-05 15:49:06",
  "stage_id": "stage_001",
  "stage_name": "逛超市",
  "stage_index": 1,
  "user_answer": "室内聚餐",
  "is_correct": false,
  "info": "structure",
  "cue_times": 0
}
```

可以聚合：

```text
按 stage_id 分组
is_correct == false 且 info != "cue_requested" → 错误次数
user_answer → 错误选项
info == "cue_requested" 或 user_answer == "请求提示" → 提示请求次数
cue_times → 当前阶段提示次数
```

但当前结构对精确反应时间不够稳定。

建议 Unity 后续增强每条 action：

```json
{
  "action_id": "xxx",
  "timestamp": "2026-05-05 15:49:06",
  "stage_id": "stage_001",
  "stage_name": "逛超市",
  "stage_index": 1,

  "scene": "scene1",
  "stage_type": "structure",
  "task_type": "selection",

  "action_type": "select_option",
  "user_answer": "室内聚餐",
  "correct_answer": "逛超市",
  "is_correct": false,

  "cue_times": 0,
  "hint_level": 0,

  "reaction_time_ms": 3200
}
```

请求提示时：

```json
{
  "action_type": "cue_requested",
  "user_answer": null,
  "is_correct": null,
  "cue_times": 3,
  "hint_level": 3,
  "reaction_time_ms": 1800
}
```

### TODO: recall-report 接口

当前建议新增：

```http
POST /api/recall-report?user=001
```

用于 Unity 上传行为日志。

保存路径建议：

```text
output/001/recall_report_raw.json
```

或暂时保存为：

```text
output/001/TaskContext.json
```

医生网页读取：

```http
GET /api/recall-report?user=001
```

第一版可以直接返回原始行为日志，前端聚合。

更稳定的方案是后端返回聚合后的医生报告结构。

---

## 13. staged / pipeline_cache 说明

当前第一版实验流程主要使用：

```text
full_context
```

也就是调用完整上下文生成逻辑，直接生成或复用：

```text
extracted_context.json
```

`staged` 模式和 `pipeline_cache` 当前更适合：

```text
开发调试
中间结果检查
未来分阶段优化
```

不建议第一版暴露给医生网页端。

医生网页端不需要理解：

```text
stage1_video_parse
stage2_event_rebuild
stage3_detail_generate
```

它只需要关心：

```text
上传视频
处理状态
处理完成
预览 context
确认 Unity 可拉取
```

---

## 14. 常见问题排查

### 14.1 前端请求 404

检查：

```text
API_BASE_URL 是否是 http://127.0.0.1:8000/api
后端是否运行 python api_server.py
接口是否带 /api 前缀
```

错误：

```text
http://127.0.0.1:8000/tasks/process
```

正确：

```text
http://127.0.0.1:8000/api/tasks/process
```

---

### 14.2 图片显示不出来

检查：

```text
是否通过 /assets/{user}/{relativePath} 请求
是否把反斜杠 \ 替换成 /
是否去掉 output/{user}/ 前缀
文件是否真实存在于 output/{user}/...
```

错误：

```tsx
<img src="frames/逛超市.jpg" />
```

正确：

```tsx
<img src={buildAssetUrl(user, "frames/逛超市.jpg")} />
```

---

### 14.3 context 页面为空

检查前端是否仍在读取旧字段：

```text
activity_theme
sub_events
key_objects
object_details
qa
```

真实结构应读取：

```text
activity
events
entities
detail_pair
```

---

### 14.4 GameMeta/GameFlow 状态灯失败

不要检查：

```text
gameMeta.success
gameFlow.success
```

真实判断：

```ts
Boolean(gameMeta?.game_levels?.length)
Boolean(gameFlow?.game_flow?.length)
```

---

### 14.5 Unity 一直 ready=false

检查：

```text
GET /api/job-status?user=001
status 是否为 all_ready
asset_validation.ok 是否为 true
GameMeta.json 是否存在
GameFlow.json 是否存在
资源文件是否缺失
```

---

### 14.6 CORS 报错

如果前端和后端不同端口，Flask 需要允许跨域：

```bash
pip install flask-cors
```

```python
from flask_cors import CORS

CORS(app)
```

---

### 14.7 大文件误提交 GitHub

不要提交：

```text
output/
videos/
lifelog.db
.env
模型缓存
日志文件
```

`.gitignore` 应覆盖：

```gitignore
output/
videos/
*.db
.env
*.log
__pycache__/
```

---

## 15. 当前不做的事情

实验第一版不做：

```text
正式登录/权限系统
医院级数据库系统
云端正式部署
多医院/多医生权限隔离
医生网页端直接编辑 Unity JSON
staged pipeline 可视化
复杂任务失败恢复
长期统计报表
```

---

## 16. 当前 TODO

```text
1. 完善 /api/recall-report
2. Unity 行为日志增加 reaction_time_ms
3. Unity 行为日志增加 action_type
4. Unity 行为日志冗余 scene / stage_type / task_type
5. shopping_list 进入 prompt，但只作为弱约束
6. 医生网页端预览以 /api/context 为主
7. 前端资源路径统一使用 /assets/{user}/{relativePath}
8. 后端生成资源路径时清洗 output\default\xxx 这类异常路径
9. 多用户 user 参数稳定化
10. status.json 与 DB pipeline_state 的职责继续收敛
```

---

## 17. 最小实验验收标准

系统能完成以下闭环即可：

```text
1. 医生网页填写用户和活动信息
2. 上传第一视角视频
3. 后端启动 full_context pipeline
4. 网页显示处理进度
5. 后端生成 extracted_context.json
6. 后端生成 GameMeta.json / GameFlow.json
7. 网页显示处理结果预览
8. Unity 输入 user_id
9. Unity 拉取 GameMeta/GameFlow 和资源
10. 老人完成回忆任务
11. Unity 保存行为日志
12. 医生网页查看用户回忆表现
```

```
```
