import json
import datetime
from jinja2 import Template
from flask import Flask, jsonify
from openai import OpenAI
    
client = OpenAI(
    base_url='https://api.siliconflow.cn/v1',
    api_key='sk-uovqjhqjdgthwxrhtdoipiphjrayqtmhmmwkicacowrmwcrf'
)

app = Flask(__name__)

class RecallAssistant:
    def __init__(self, memory_perf_path, task_context_path, activity_context_path):
        """初始化并加载上下文"""
        with open(memory_perf_path, 'r', encoding='utf-8') as f:
            self.memory_perf = json.load(f)
        with open(task_context_path, 'r', encoding='utf-8') as f:
            self.task_context = json.load(f)
        with open(activity_context_path, 'r', encoding='utf-8') as f:
            self.activity_context = json.load(f)
        
        self.events = self.activity_context.get("events", {})
        self.activity = self.activity_context.get("activity", {})
        self.model_name = "tencent/Hunyuan-MT-7B"
        self.response_timeout = 10
    
    def get_stage(self):
        """从task_context里获取当前任务阶段信息
        """
        return self.task_context['task_stage']['stage']
    
    def get_memory_state(self):
        """分析用户记忆状态"""
        last_response_str = self.memory_perf['response_metrics']['last_response_timestamp']
        current_time = datetime.datetime.now(datetime.timezone.utc)
        # mockup
        # current_time = datetime.datetime.fromisoformat("2024-06-15T17:05:20+8:00")
        try:
            last_response_time = datetime.datetime.fromisoformat(last_response_str)
            time_diff = (current_time - last_response_time).total_seconds()
        except ValueError:
            time_diff = 0
        
        # 判断逻辑：当前错误>0 或 请求提示 或 超过10s未响应则判定为负向状态
        if (self.memory_perf['error_metrics']['current_errors'] > 0 or
            self.memory_perf['hint_metrics']['hint_requested'] or
            time_diff > self.response_timeout):
            return "negative"
        return "positive"
    
    def select_strategy(self, stage, memory_state):
        """根据任务阶段和记忆状态选择对话策略组件"""
        strategy_matrix = {
            "start": {
                "positive": ["探询"]
            },
            "in_progress": {
                "positive": ["祝贺", "重复"],
                "negative": ["提示", "安慰", "校正"]
            },
            "ending": {
                "positive": ["总结", "祝贺"],
                "negative": ["安慰", "总结"]
            }
        }
        return strategy_matrix.get(stage, {}).get(memory_state, ["探询"]) 
    
    def get_clues(self):
        """根据当前任务上下文和记忆表现获取渐进式线索"""
        current_level = self.task_context['current_level']['name']
        hint_count = self.memory_perf['hint_metrics']['hint_count']
        error_count = self.memory_perf['error_metrics']['current_errors']
        
        clue_level = min(max(hint_count, error_count), 2)
 
        if current_level == "framework":
            return self.get_theme_clues(clue_level)
        elif current_level == "narrative":
            return self.get_scene_clues(clue_level)
        elif current_level == "entity":
            return self.get_entity_clues(clue_level)
        else:
            return {"text": [], "visual": [], "audio": []}
        
    def get_theme_clues(self, clue_level):
        """框架层线索：活动主题提示"""
        clues = {"text": [], "visual": [], "audio": []}
        activity = self.activity
        
        if clue_level == 0:
            clues['text'] = [
                f"{activity['time_range']}",
                f"{activity['location']}"
            ]
        elif clue_level == 1:
            clues['audio'] = activity.get('audio_clues', [])
            if not clues['audio']:
                clue_level = 2
                clues['visual'] = activity.get('visual_cues', [])
        else:
            clues['visual'] = activity.get('visual_cues', [])
        
        return clues
     
    def get_scene_clues(self, clue_level): 
        """叙事层线索：场景/行为提示""" 
        clues = {"text": [], "visual": [], "audio": []}
        current_scene = self.task_context['retrieval_context']['scene']
        scene = self.events.get(current_scene, {})
        
        if clue_level == 0:
            clues['visual'] = scene.get('visual_cues', [])
        elif clue_level == 1:
            clues['audio'] = scene.get('audio_clues', [])
            if not clues['audio']:
                clue_level = 2
        else: 
            clues['text'] = scene.get('text_clues', [])
        
        return clues
    
    def get_entity_clues(self, clue_level):
        """实体层线索：人物/物体提示"""
        clues = {"text": [], "visual": [], "audio": []}
        scene_name = self.task_context['retrieval_context']['scene']
        target_name = self.task_context['retrieval_context']['current_target']
        scene = self.events.get(scene_name, {})
        target = (
            scene.get("key_objects", {}).get(target_name)
            or scene.get("key_persons", {}).get(target_name)
        )
        if not target:
            return clues

        if clue_level == 0:
            clues['text'] = target.get('text_clues', []) 
        elif clue_level == 1: 
            clues['audio'] = target.get('audio_clues', [])
            if not clues['audio']: 
                clues['visual'] = target.get('visual_clues', []) 
                if not clues['visual']: 
                    clues['text'] = target.get('text_clues', []) 
        else: 
            entity_images = target.get('entity_images', [])
            if not entity_images:
                enhanced = target.get("enhanced_visual_clues", [])
                entity_images = [
                    item.get("output_path")
                    for item in enhanced
                    if item.get("output_path")
                ]
            if entity_images: 
                bounding_box = target['bounding_box']
                 
                clues['visual'] = [ 
                    { 
                        "image": entity_images[0], 
                        "type": "progressive", 
                        "steps": [ 
                            {"time": 0, "action": "mask", "area": bounding_box}, 
                            {"time": 5, "action": "reveal"}, 
                            {"time": 10, "action": "highlight", "area": bounding_box}
                        ]
                    }
                ]
            else: 
                clues['text'] = [f"{target['target_description']}"]
         
        return clues
 
    def generate_guide_prompt(self, strategy_components, clues): 
        """构建记忆引导提示词""" 
        # 提取上下文信息 
        context = self.task_context['retrieval_context']
        perf = self.memory_perf
        key_persons = [
            name
            for event in self.events.values()
            for name in event.get("key_persons", {}).keys()
        ]
        key_objects = [
            name
            for event in self.events.values()
            for name in event.get("key_objects", {}).keys()
        ]
        with open("prompts/strategy_components.json", "r", encoding="utf-8") as f:
            strategy_components_rules = json.load(f)
            rules = []
            for i in strategy_components:
                rules.append(strategy_components_rules.get(i))

        # 准备提示词变量
        prompt_vars = {
            "task_level": self.task_context['current_level']['name'],
            "task_type": self.task_context['current_task']['task_type'],
            "activity_theme": context['parent_activity']['theme'],
            "activity_time": context['parent_activity']['time_range'],
            "activity_loc": context['parent_activity']['location'],
            "task_target": context['current_target'],
            "events": list(self.events.keys()),
            "key_persons": key_persons,
            "key_objects": key_objects,
            "retrieved_item": [item['item'] for item in perf['current_level_progress']['retrieved_items']],
            "wrong_item": [item['item'] for item in perf['current_level_progress']['wrong_items']],
            "clues": clues,
            "strategy": ",".join(strategy_components),
            "component_rules": "\n  ".join(rules)
        }
        
        with open("prompts/strategy_prompt.md", "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(**prompt_vars)

        return prompt
     
    def get_response(self):
        """生成记忆引导内容"""
        # 获取任务阶段和记忆状态
        stage = self.get_stage()
        memory_state = self.get_memory_state() if stage == "in_progress" else None
        
        # 选择策略并获取线索
        strategy_components = self.select_strategy(stage, memory_state)
        clues = self.get_clues() if memory_state == "negative" else None
        
        # 生成引导提示词
        prompt = self.generate_guide_prompt(strategy_components, clues)

        # 调用API生成引导内容
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", 
                           "content": prompt},
                        #   {'role': 'user', 
                        #    'content': "推理模型会给市场带来哪些新的机会"}
                ],
                temperature=0.3,
                max_tokens=1000
            )
            if response.choices[0].message.content:
                print(response.choices[0].message.content)
                guide_content = json.loads(response.choices[0].message.content)
            else:
                print("no content")
                guide_content = {"dialogues": [{
                "id": 1,
                "strategy": "",
                "content": ""
            }]}
        except Exception as e:
            # API调用失败时返回默认引导
            print(e)
            guide_content = {"dialogues": [{
                "id": 1,
                "strategy": "",
                "content": ""
            }]}
         
        # 构建最终响应
        return {
            "stage": stage,
            "memory_state": memory_state,
            "strategies": strategy_components,
            "clues": clues,
            "guide_content": guide_content
        }

@app.route('/get_guide', methods=['GET'])
def get_guide():
    assistant = RecallAssistant('user_data/memory_performance.json',
                                'user_data/task_context.json',
                                "user_data/activity_context.json")
    response = assistant.get_response()
    return jsonify(response)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
