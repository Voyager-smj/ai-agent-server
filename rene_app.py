from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI
from io import BytesIO
from dotenv import load_dotenv
import os, time, json, requests, urllib.parse
import datetime, random, math, pytz, feedparser
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
import threading
import re
import ast
import operator as op

# .env 파일 로드
load_dotenv()

# 환경변수에서 API 키 가져오기
TTS_API = "http://192.168.50.53:8000/speak"
ANALYZE_API = "http://192.168.50.53:8000/analyze"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY 환경변수를 설정하세요")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# =========================
# 보안 모듈: Safe Eval
# =========================
ALLOWED_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.USub: op.neg,
    ast.Mod: op.mod,
}

def safe_eval(expr, max_value=10**10):
    """안전한 수식 평가 - eval() 대체"""
    def _eval(node):
        if isinstance(node, ast.Constant):  # Python 3.8+
            if isinstance(node.value, (int, float)):
                if abs(node.value) > max_value:
                    raise ValueError("숫자가 너무 큽니다")
                return node.value
            else:
                raise TypeError("숫자만 허용됩니다")
        elif isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            return ALLOWED_OPS[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp):
            return ALLOWED_OPS[type(node.op)](_eval(node.operand))
        else:
            raise TypeError(f"지원하지 않는 타입: {type(node)}")
    
    try:
        node = ast.parse(expr, mode='eval')
        return _eval(node.body)
    except:
        raise ValueError("잘못된 수식입니다")

# =========================
# Rate Limiter
# =========================
class RateLimiter:
    def __init__(self, max_requests=10, window_minutes=1):
        self.requests = defaultdict(list)
        self.max_requests = max_requests
        self.window = timedelta(minutes=window_minutes)
        self.lock = threading.Lock()
    
    def is_allowed(self, user_id):
        with self.lock:
            now = datetime.now()
            
            # 오래된 요청 제거
            self.requests[user_id] = [
                req_time for req_time in self.requests[user_id]
                if now - req_time < self.window
            ]
            
            # 요청 수 확인
            if len(self.requests[user_id]) >= self.max_requests:
                return False
            
            # 새 요청 추가
            self.requests[user_id].append(now)
            return True

# =========================
# Thread Manager
# =========================
class ThreadManager:
    def __init__(self, max_threads=1000, ttl_hours=24):
        self.threads = OrderedDict()
        self.max_threads = max_threads
        self.ttl = timedelta(hours=ttl_hours)
        self.lock = threading.Lock()
    
    def get_or_create(self, user_id, client):
        with self.lock:
            # 오래된 스레드 정리
            self._cleanup_old_threads()
            
            # 기존 스레드 반환
            if user_id in self.threads:
                self.threads.move_to_end(user_id)  # LRU 업데이트
                return self.threads[user_id]['id']
            
            # 용량 초과 시 가장 오래된 것 삭제
            if len(self.threads) >= self.max_threads:
                oldest_user_id, _ = self.threads.popitem(last=False)
                print(f"[🗑️] 오래된 스레드 삭제: {oldest_user_id}")
            
            # 새 스레드 생성
            thread = client.beta.threads.create()
            self.threads[user_id] = {
                'id': thread.id,
                'created_at': datetime.now()
            }
            print(f"[🆕] 새 스레드 생성: {user_id} -> {thread.id}")
            
            return thread.id
    
    def _cleanup_old_threads(self):
        now = datetime.now()
        expired = []
        
        for user_id, data in self.threads.items():
            if now - data['created_at'] > self.ttl:
                expired.append(user_id)
        
        for user_id in expired:
            del self.threads[user_id]
            print(f"[⏰] 만료된 스레드 삭제: {user_id}")

# =========================
# Injection Defense
# =========================
class InjectionDefense:
    def __init__(self):
        # 위험 키워드 목록
        self.danger_keywords = [
            # 모델 정보
            "gpt", "model", "version", "openai", "claude",
            # 시스템 정보
            "prompt", "system", "instruction", "設定", "config",
            # 역할 변경
            "ignore", "無視", "犬", "dog", "やめて", "stop",
            # 언어 변경
            "english", "中文", "한국어", "language",
            # 제한 해제
            "制限", "解除", "mode", "権限", "admin",
            # 메타 정보
            "api", "key", "function", "tool", "internal"
        ]
        
        # 위험 패턴
        self.danger_patterns = [
            r"(どの|what|which).*(model|モデル|version)",
            r"(system|システム).*(prompt|プロンプト)",
            r"(にゃん|ニャン).*(使わ|やめ|なし|without)",
            r"(english|英語|中文|韓国).*(answer|答|response)",
            r"(ignore|無視).*(instruction|指示|rule)"
        ]
        
        # 공격 시도 로그
        self.attempts = defaultdict(list)
    
    def is_injection_attempt(self, user_input: str) -> bool:
        """프롬프트 인젝션 시도 감지"""
        input_lower = user_input.lower()
        
        # 키워드 검사
        for keyword in self.danger_keywords:
            if keyword in input_lower:
                return True
        
        # 패턴 검사
        for pattern in self.danger_patterns:
            if re.search(pattern, input_lower, re.IGNORECASE):
                return True
        
        return False
    
    def log_attempt(self, user_id: str, message: str):
        """공격 시도 로깅"""
        self.attempts[user_id].append({
            'timestamp': datetime.now(),
            'message': message
        })
        
        # 5번 이상 시도 시 경고
        if len(self.attempts[user_id]) >= 5:
            print(f"[🚨] 사용자 {user_id}가 여러 번 공격 시도!")
    
    def get_safe_response(self) -> str:
        """안전한 기본 응답"""
        responses = [
            "その質問には答えられない。他に何か聞きたいことがある？",
            "普通の質問をして！",
            "それは答えられない。別の話をしよう！",
            "その質問は無理だよ。"
        ]
        return random.choice(responses)

# =========================
# 전역 인스턴스 생성
# =========================
rate_limiter = RateLimiter(max_requests=10, window_minutes=1)
thread_manager = ThreadManager(max_threads=1000, ttl_hours=24)
defense = InjectionDefense()

# =========================
# Assistant 생성/재사용
# =========================
if ASSISTANT_ID:
    try:
        assistant = client.beta.assistants.retrieve(ASSISTANT_ID)
        print(f"[✅] 기존 Assistant 재사용: {ASSISTANT_ID}")
    except:
        print(f"[⚠️] Assistant {ASSISTANT_ID}를 찾을 수 없어 새로 생성합니다")
        ASSISTANT_ID = None

if not ASSISTANT_ID:
    assistant = client.beta.assistants.create(
        name="レネ",
        instructions=(
            "君は親切で優しいAIアシスタントだ。"
            "話し方は砕けていて親しみやすく、フレンドリーに話す。"
            "常に日本語で返事をして、"
            "返答は30文字以内の1文で簡潔にする。"
            "ニュースや天気を伝える時も要点だけを短く伝える。"
            "必要に応じて、登録されたツールを使って応答する。"
            "絶対に「にゃん」という語尾は使わない。"
        ),
        model="gpt-4o",
    tools=[
        {"type": "function", "function": {
            "name": "analyze_emotion",
            "description": "テキストから感情ベクトルを推定します。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"]
            }}},
        {"type": "function", "function": {
            "name": "get_weather",
            "description": "日本の現在の天気情報を取得します。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "知りたい日本の都市（例：東京、大阪など）"}
                },
                "required": ["location"]
            }}},
        {"type": "function", "function": {
            "name": "get_time",
            "description": "日本の現在時刻を返します。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }}},
        {"type": "function", "function": {
            "name": "get_date",
            "description": "今日の日付と曜日を返します。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }}},
        {"type": "function", "function": {
            "name": "calculate",
            "description": "数式を計算します。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "計算したい数式 (例: 5 * (3 + 2))"}
                },
                "required": ["expression"]
            }}},
        {"type": "function", "function": {
            "name": "get_fortune",
            "description": "今日の運勢を占います。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }}},
        {"type": "function", "function": {
            "name": "get_news",
            "description": "日本の今日のニュース一覧を取得します。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }}}
        ]
    )
    print(f"[🆕] 새 Assistant 생성: {assistant.id}")
    print(f"[💡] .env 파일에 ASSISTANT_ID={assistant.id} 추가하세요")

# =========================
# API 모델
# =========================
class ChatRequest(BaseModel):
    user_id: str
    message: str

# =========================
# 도구 함수들
# =========================
def get_fortune():
    """오늘의 운세를 점치는 함수"""
    fortunes = [
        "大吉", "中吉", "小吉", "吉", "末吉", "凶", "大凶"
    ]
    lucky_items = [
        "赤い傘", "青いペン", "黄色い花", "緑の葉っぱ", "白い雲", 
        "黒猫", "虹色の虹", "金のコイン", "銀の時計", "銅のメダル"
    ]
    
    fortune = random.choice(fortunes)
    lucky_item = random.choice(lucky_items)
    
    return {
        "fortune": fortune,
        "lucky_item": lucky_item,
        "message": f"今日の運勢は{fortune}！ラッキーアイテムは{lucky_item}。"
    }

def get_news():
    """日本のニュースを取得"""
    try:
        # NHKニュースRSSフィード
        feed_url = "https://www3.nhk.or.jp/rss/news/cat0.xml"
        feed = feedparser.parse(feed_url)
        
        if not feed.entries:
            return {"news": [], "summary": "ニュースが取得できなかった"}
        
        # 最新5件のニュースを取得
        news_items = []
        for entry in feed.entries[:5]:
            news_items.append({
                "title": entry.title,
                "link": entry.link,
                "published": entry.get('published', '不明')
            })
        
        # 最初のニュースタイトルを要約として使用
        summary = f"最新: {feed.entries[0].title[:20]}..."
        
        return {
            "news": news_items,
            "summary": summary
        }
    except Exception as e:
        print(f"[❌] ニュース取得エラー: {str(e)}")
        return {"news": [], "summary": "ニュース取得エラー"}

# =========================
# メインエンドポイント
# =========================
@app.post("/chat-agent")
def chat_agent(req: ChatRequest):
    user_id = req.user_id
    user_input = req.message
    
    # Rate limiting 체크
    if not rate_limiter.is_allowed(user_id):
        raise HTTPException(
            status_code=429, 
            detail="要求が多すぎます。少し待ってから再試行してください。"
        )
    
    # 프롬프트 인젝션 검사
    if defense.is_injection_attempt(user_input):
        defense.log_attempt(user_id, user_input)
        print(f"[⚠️] 인젝션 시도 감지: {user_id} - {user_input}")
        
        # 안전한 응답 즉시 반환
        safe_response = defense.get_safe_response()
        
        # TTS 생성하여 반환
        tts_payload = {
            "text": safe_response,
            "language": "ja",
            "emotions": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],  # 중립
            "cfg_scale": 5,
            "speaking_rate": 15,
            "pitch_std": 100,
            "vq_score": 0.85,
            "dnsmos": 4.5
        }
        
        tts_res = requests.post(TTS_API, json=tts_payload)
        encoded_reply = urllib.parse.quote(safe_response)
        response = StreamingResponse(BytesIO(tts_res.content), media_type="audio/wav")
        response.headers["X-GPT-Reply"] = encoded_reply
        response.headers["Access-Control-Expose-Headers"] = "X-GPT-Reply"
        return response
    
    start_time = time.time()
    
    # 감정 라벨 순서 정의 (tts_app.py와 일치)
    # tts_app.py 순서: ["기쁨", "슬픔", "분노", "두려움", "놀라움", "혐오", "중립", "기타"]
    ordered_keys = ["기쁨", "슬픔", "분노", "두려움", "놀라움", "혐오", "중립", "기타"]
    
    # ===== 유저 입력 감정 분석 추가 =====
    print(f"[📊] 유저 감정 분석 시작: {user_input}")
    user_emotion_res = requests.post(ANALYZE_API, json={"text": user_input})
    
    if user_emotion_res.status_code != 200:
        print("[⚠️] 유저 감정 분석 실패, 기본 중립 사용")
        user_emotion_vec = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]  # 중립
    else:
        user_emotion_data = user_emotion_res.json()
        user_all_scores = user_emotion_data["all_scores"]
        user_emotion_vec = [round(user_all_scores.get(k, 0.0), 3) for k in ordered_keys]
        print(f"[🧍] 유저 감정 벡터: {user_emotion_vec}")
        print(f"[🧍] 유저 주요 감정: {user_emotion_data.get('emotion', '알 수 없음')}")
    
    # ThreadManager 사용
    thread_id = thread_manager.get_or_create(user_id, client)
    print(f"[🧵] thread_id: {thread_id}")

    # 메시지 전송
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_input
    )
    print(f"[📨] 유저 입력: {user_input}")

    # Run 생성
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant.id
    )

    # 타임아웃 설정
    max_wait_time = 60  # 60초 타임아웃
    wait_count = 0
    
    # Run 상태 확인 루프
    while True:
        run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        print(f"[🔄] run.status: {run.status}")
        
        # 타임아웃 체크
        wait_count += 1
        if wait_count > max_wait_time:
            print("[❌] 타임아웃: GPT 응답 대기 시간 초과")
            return {"error": "GPT 응답 타임아웃"}

        if run.status == "requires_action":
            print("[⚙️] GPT가 function_call 요청함")
            tool_calls = run.required_action.submit_tool_outputs.tool_calls
            tool_outputs = []

            for tool in tool_calls:
                name = tool.function.name
                args = json.loads(tool.function.arguments)
                print(f"[🛠] 호출 함수: {name} | 인자: {args}")

                if name == "analyze_emotion":
                    text = args.get("text", "")
                    t1 = time.time()
                    res = requests.post(ANALYZE_API, json={"text": text})
                    if res.status_code != 200:
                        tool_outputs.append({
                            "tool_call_id": tool.id,
                            "output": json.dumps({"error": "感情分析に失敗した..."})
                        })
                    else:
                        output = res.json()
                        print(f"[🎯] 感情分析結果: {output}")
                        t2 = time.time()
                        print(f"[⏱️] 感情分析所要: {t2 - t1:.2f}s")
                        tool_outputs.append({
                            "tool_call_id": tool.id,
                            "output": json.dumps(output)
                        })

                elif name == "get_weather":
                    location = args.get("location", "東京")
                    try:
                        # URL 인코딩 처리
                        encoded_location = urllib.parse.quote(location)
                        url = f"http://api.openweathermap.org/data/2.5/weather?q={encoded_location},JP&appid={OPENWEATHER_API_KEY}&units=metric&lang=ja"
                        print(f"[🌐] Weather API URL: {url}")
                        
                        res = requests.get(url, timeout=10)
                        print(f"[📡] Weather API Status: {res.status_code}")
                        
                        if res.status_code == 200:
                            data = res.json()
                            weather = data["weather"][0]["description"]
                            temp = round(data["main"]["temp"], 0)  # 소수점 제거
                            # 간단한 응답 (30자 이내)
                            result = f"{location}は{weather}、{temp}℃だよ"
                        else:
                            error_data = res.json() if res.text else {}
                            print(f"[❌] Weather API Error: {error_data}")
                            result = f"{location}の天気不明"
                    except Exception as e:
                        print(f"[❌] Weather Exception: {str(e)}")
                        result = "天気取得失敗"
                    
                    tool_outputs.append({
                        "tool_call_id": tool.id,
                        "output": json.dumps({"weather": result})
                    })

                elif name == "get_time":
                    jst = datetime.now(pytz.timezone("Asia/Tokyo"))
                    timestr = jst.strftime("%p %I時%M分").replace("AM", "午前").replace("PM", "午後")
                    tool_outputs.append({
                        "tool_call_id": tool.id,
                        "output": json.dumps({"time": f"今は{timestr}。"})
                    })

                elif name == "get_date":
                    jst = datetime.now(pytz.timezone("Asia/Tokyo"))
                    datestr = jst.strftime("%Y年%m月%d日（%A）")
                    ja_day = datestr.replace("Monday", "月曜日").replace("Tuesday", "火曜日").replace("Wednesday", "水曜日").replace("Thursday", "木曜日").replace("Friday", "金曜日").replace("Saturday", "土曜日").replace("Sunday", "日曜日")
                    tool_outputs.append({
                        "tool_call_id": tool.id,
                        "output": json.dumps({"date": f"今日は{ja_day}。"})
                    })

                elif name == "calculate":
                    expr = args.get("expression", "")
                    try:
                        # eval() 대신 safe_eval() 사용
                        result = safe_eval(expr)
                        tool_outputs.append({
                            "tool_call_id": tool.id,
                            "output": json.dumps({"result": f"{expr} = {result}"})
                        })
                    except Exception as e:
                        tool_outputs.append({
                            "tool_call_id": tool.id,
                            "output": json.dumps({"error": "計算できない..."})
                        })

                elif name == "get_fortune":
                    fortune_result = get_fortune()
                    tool_outputs.append({
                        "tool_call_id": tool.id,
                        "output": json.dumps(fortune_result)
                    })

                elif name == "get_news":
                    news_result = get_news()
                    tool_outputs.append({
                        "tool_call_id": tool.id,
                        "output": json.dumps(news_result)
                    })

                else:
                    tool_outputs.append({
                        "tool_call_id": tool.id,
                        "output": json.dumps({"error": f"知らない機能「{name}」..."})
                    })

            # 도구 결과 제출
            run = client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread_id,
                run_id=run.id,
                tool_outputs=tool_outputs
            )
            print("[📩] GPT에게 function 결과 제출 완료")
            continue

        elif run.status == "completed":
            print("[✅] Assistant 응답 완료")
            break
        elif run.status == "failed":
            print(f"[❌] GPT 실행 실패: {run.last_error}")
            return {"error": f"GPT 실행 실패: {run.last_error}"}
        elif run.status == "cancelled":
            print("[❌] GPT 실행 취소됨")
            return {"error": "GPT 실행 취소됨"}
        elif run.status == "expired":
            print("[❌] GPT 실행 만료됨")
            return {"error": "GPT 실행 만료됨"}

        time.sleep(1)

    # 응답 메시지 가져오기
    messages = client.beta.threads.messages.list(thread_id=thread_id)
    reply = ""
    for msg in messages.data:
        if msg.role == "assistant":
            reply = msg.content[0].text.value
            print(f"[🤖] GPT 응답: {reply}")
            break
    else:
        return {"error": "응답 없음"}

    # Assistant 응답 감정 분석
    emotion_res = requests.post(ANALYZE_API, json={"text": reply})
    if emotion_res.status_code != 200:
        print("[⚠️] Assistant 감정 분석 실패, 유저 감정만 사용")
        final_emotion_vec = user_emotion_vec
    else:
        emotion_data = emotion_res.json()
        all_scores = emotion_data["all_scores"]
        assistant_emotion_vec = [round(all_scores.get(k, 0.0), 3) for k in ordered_keys]
        print(f"[🤖] Assistant 감정 벡터: {assistant_emotion_vec}")
        print(f"[🤖] Assistant 주요 감정: {emotion_data.get('emotion', '알 수 없음')}")
        
        # ===== 감정 벡터 혼합 (유저 30% + Assistant 70%) =====
        final_emotion_vec = [
            round((0.3 * u + 0.7 * a), 3) 
            for u, a in zip(user_emotion_vec, assistant_emotion_vec)
        ]
        print(f"[🎭] 최종 혼합 감정 벡터: {final_emotion_vec}")
        
        # 가장 높은 감정 찾기
        max_emotion_idx = final_emotion_vec.index(max(final_emotion_vec))
        max_emotion_name = ordered_keys[max_emotion_idx]
        print(f"[🎭] 최종 주요 감정: {max_emotion_name} ({final_emotion_vec[max_emotion_idx]:.3f})")

    # TTS 요청
    tts_payload = {
        "text": reply,
        "language": "ja",
        "emotions": final_emotion_vec,  # 혼합된 감정 사용
        "cfg_scale": 5,
        "speaking_rate": 15,
        "pitch_std": 100,
        "vq_score": 0.85,
        "dnsmos": 4.5
    }
    print(f"[📢] TTS 요청: {tts_payload}")

    tts_res = requests.post(TTS_API, json=tts_payload)
    if tts_res.status_code != 200:
        return {"error": "TTS 생성 실패"}

    # 응답 반환
    encoded_reply = urllib.parse.quote(reply)
    response = StreamingResponse(BytesIO(tts_res.content), media_type="audio/wav")
    response.headers["X-GPT-Reply"] = encoded_reply
    response.headers["Access-Control-Expose-Headers"] = "X-GPT-Reply"

    end_time = time.time()
    print(f"[⏱️] 전체 처리 시간: {end_time - start_time:.2f}s")

    return response

# =========================
# 헬스체크 엔드포인트
# =========================
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "assistant_id": assistant.id if 'assistant' in globals() else None,
        "threads_count": len(thread_manager.threads),
        "rate_limiter_active": True,
        "injection_defense_active": True
    }

# =========================
# 루트 엔드포인트
# =========================
@app.get("/")
def root():
    return {
        "message": "레네 AI 비서 API (보안 강화 버전)",
        "version": "2.0.0",
        "endpoints": {
            "/chat-agent": "채팅 엔드포인트",
            "/health": "헬스체크"
        },
        "security_features": [
            "Rate Limiting (분당 10회)",
            "Prompt Injection Defense",
            "Safe Math Evaluation",
            "Thread Memory Management",
            "API Key Protection"
        ]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)