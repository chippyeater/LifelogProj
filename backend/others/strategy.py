import json
import datetime
import os
import logging
from dotenv import load_dotenv
from jinja2 import Template
from flask import Flask, jsonify
from openai import OpenAI
from runtime_config import get_config_value, resolve_prompt_path

load_dotenv()  # 鍔犺浇 .env 鏂囦欢涓殑鐜鍙橀噺

client = OpenAI(
    base_url=str(get_config_value("models.siliconflow.base_url", "https://api.siliconflow.cn/v1")),
    api_key=get_config_value("models.siliconflow.api_key")
)

app = Flask(__name__)
logger = logging.getLogger(__name__)

class RecallAssistant:
    def __init__(self, memory_perf_path, task_context_path, activity_context_path):
        """
        鍒濆鍖栧苟鍔犺浇涓婁笅鏂囷紝璇诲彇锛?
        - memory_performance.json锛堟彁绀烘鏁般€侀敊璇鏁般€佽〃鐜扮瓑锛?
        - task_context.json锛堝綋鍓嶅洖蹇嗕换鍔″眰娆★細framework/narrative/entity锛宺etrieval_context 绛夛級
        - activity_context.json锛堜簨浠?鍦烘櫙/绾跨储锛?
        """
        with open(memory_perf_path, 'r', encoding='utf-8') as f:
            self.memory_perf = json.load(f)
        with open(task_context_path, 'r', encoding='utf-8') as f:
            self.task_context = json.load(f)
        with open(activity_context_path, 'r', encoding='utf-8') as f:
            self.activity_context = json.load(f)
        
        self.events = self.activity_context.get("events", {})
        self.activity = self.activity_context.get("activity", {})
        self.model_name = str(get_config_value("models.siliconflow.model", "tencent/Hunyuan-MT-7B"))
        self.response_timeout = float(get_config_value("strategy.response_timeout_seconds", 10))
    
    def get_stage(self):
        """浠巘ask_context閲岃幏鍙栧綋鍓嶄换鍔￠樁娈典俊鎭細starting / in_progress / ending
        """
        return self.task_context['task_stage']['stage']
    
    def get_memory_state(self):
        """
        鍒嗘瀽鐢ㄦ埛璁板繂鐘舵€?
        鏍规嵁閿欒鏁?鎻愮ず鏁颁箣绫绘槧灏勬垚 positive / negative
        """
        last_response_str = self.memory_perf['response_metrics']['last_response_timestamp']
        current_time = datetime.datetime.now(datetime.timezone.utc)
        # mockup
        # current_time = datetime.datetime.fromisoformat("2024-06-15T17:05:20+8:00")
        try:
            last_response_time = datetime.datetime.fromisoformat(last_response_str)
            time_diff = (current_time - last_response_time).total_seconds()
        except ValueError:
            time_diff = 0
        
        # 鍒ゆ柇閫昏緫锛氬綋鍓嶉敊璇?0 鎴?璇锋眰鎻愮ず 鎴?瓒呰繃10s鏈搷搴斿垯鍒ゅ畾涓鸿礋鍚戠姸鎬?
        if (self.memory_perf['error_metrics']['current_errors'] > 0 or
            self.memory_perf['hint_metrics']['hint_requested'] or
            time_diff > self.response_timeout):
            return "negative"
        return "positive"
    
    def select_strategy(self, stage, memory_state):
        """鏍规嵁浠诲姟闃舵鍜岃蹇嗙姸鎬侀€夋嫨瀵硅瘽绛栫暐缁勪欢"""
        strategy_matrix = {
            "start": {
                "positive": ["鎺㈣"]
            },
            "in_progress": {
                "positive": ["绁濊春", "閲嶅"],
                "negative": ["鎻愮ず", "瀹夋叞", "鏍℃"]
            },
            "ending": {
                "positive": ["鎬荤粨", "绁濊春"],
                "negative": ["瀹夋叞", "鎬荤粨"]
            }
        }
        return strategy_matrix.get(stage, {}).get(memory_state, ["鎺㈣"]) 
    
    def get_clues(self):
        """
        鏍规嵁褰撳墠浠诲姟涓婁笅鏂囧拰璁板繂琛ㄧ幇鑾峰彇娓愯繘寮忕嚎绱?
        绾跨储鈥滃己搴︹€濈敤 hint_count + error_count 绮楃暐璁＄畻 clue_level锛?~2锛?
        """
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
        """妗嗘灦灞傜嚎绱細娲诲姩涓婚鎻愮ず"""
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
        """鍙欎簨灞傜嚎绱細鍦烘櫙/琛屼负鎻愮ず""" 
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
        """瀹炰綋灞傜嚎绱細浜虹墿/鐗╀綋鎻愮ず"""
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
            enhanced_path = target.get("enhanced_image_path")
            if not enhanced_path:
                enhanced = target.get("enhanced_visual_clues", [])
                enhanced_path = next(
                    (item.get("output_path") for item in enhanced if item.get("output_path")), None
                )
            if enhanced_path: 
                bounding_box = target['bounding_box']
                 
                clues['visual'] = [ 
                    { 
                        "image": enhanced_path, 
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
        """鏋勫缓璁板繂寮曞鎻愮ず璇?"" 
        # 鎻愬彇涓婁笅鏂囦俊鎭?
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
        with open(resolve_prompt_path("strategy_components"), "r", encoding="utf-8") as f:
            strategy_components_rules = json.load(f)
            rules = []
            for i in strategy_components:
                rules.append(strategy_components_rules.get(i))

        # 鍑嗗鎻愮ず璇嶅彉閲?
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
        
        with open(resolve_prompt_path("strategy"), "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(**prompt_vars)

        return prompt
     
    def get_response(self):
        """鐢熸垚璁板繂寮曞鍐呭"""
        # 鑾峰彇浠诲姟闃舵鍜岃蹇嗙姸鎬?
        stage = self.get_stage()
        memory_state = self.get_memory_state() if stage == "in_progress" else None
        
        # 閫夋嫨绛栫暐骞惰幏鍙栫嚎绱?
        strategy_components = self.select_strategy(stage, memory_state)
        clues = self.get_clues() if memory_state == "negative" else None
        
        # 鐢熸垚寮曞鎻愮ず璇?
        prompt = self.generate_guide_prompt(strategy_components, clues)

        # 璋冪敤API鐢熸垚寮曞鍐呭
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", 
                           "content": prompt},
                        #   {'role': 'user', 
                        #    'content': "鎺ㄧ悊妯″瀷浼氱粰甯傚満甯︽潵鍝簺鏂扮殑鏈轰細"}
                ],
                temperature=0.3,
                max_tokens=1000
            )
            if response.choices[0].message.content:
                logger.info(response.choices[0].message.content)
                guide_content = json.loads(response.choices[0].message.content)
            else:
                logger.warning("no content")
                guide_content = {"dialogues": [{
                "id": 1,
                "strategy": "",
                "content": ""
            }]}
        except Exception as e:
            # API璋冪敤澶辫触鏃惰繑鍥為粯璁ゅ紩瀵?
            logger.exception("Guide generation failed: %s", e)
            guide_content = {"dialogues": [{
                "id": 1,
                "strategy": "",
                "content": ""
            }]}
         
        # 鏋勫缓鏈€缁堝搷搴?
        return {
            "stage": stage,
            "memory_state": memory_state,
            "strategies": strategy_components,
            "clues": clues,
            "guide_content": guide_content
        }

"""
鎺ュ彛锛氳繑鍥烇細stage銆乵emory_state銆侀€夌敤绛栫暐缁勪欢銆佺嚎绱€佷互鍙婃渶缁堝璇濆紩瀵煎唴瀹?
"""
@app.route('/get_guide', methods=['GET'])
def get_guide():
    """
    get_guide 鐨?Docstring
    姣忔璇锋眰 new 涓€涓?RecallAssistant锛岀洿鎺ヨ繑鍥?assistant.get_response()
    """
    assistant = RecallAssistant('user_data/memory_performance.json',
                                'user_data/task_context.json',
                                "user_data/activity_context.json")
    response = assistant.get_response()
    return jsonify(response)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

