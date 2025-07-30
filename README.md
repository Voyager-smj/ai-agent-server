# 🐾 Rene GPT Agent Server

> "にゃん"을 말버릇으로 가진, 감정을 이해하고 음성으로 말하는 일본어 고양이 NPC 챗봇 서버

---

## 🧩 주요 기능

- 🧠 GPT-4o 기반 대화 + 도구(Function Call)
- 🎭 감정 분석 (자체 모델)
- 🔊 감정 TTS 음성 응답
- 🛡️ 보안 기능: Prompt Injection 방어, Rate Limiting
- 🔗 기능 툴: `get_time`, `get_weather`, `get_date`, `calculate`, `get_fortune`, `get_news`, `analyze_emotion`

---

## 🚀 빠른 시작

```bash
git clone https://github.com/your-org/rene-agent-server.git
cd rene-agent-server
python -m venv venv
source venv/bin/activate  # (Windows: venv\Scripts\activate)
pip install -r requirements.txt
```

### 1. `.env` 파일 만들기

```env
OPENAI_API_KEY=sk-...
OPENWEATHER_API_KEY=...
ASSISTANT_ID=   # 최초 실행 시 자동 생성됨
```

### 2. 서버 실행

```bash
uvicorn rene_app:app --host 0.0.0.0 --port=8000
```

---

## 🧪 API 사용법

### 🔁 `/chat-agent` (POST)

GPT + 감정 분석 + TTS까지 포함된 주 대화 API입니다.

#### ✅ 요청 예시

```json
POST /chat-agent
{
  "user_id": "captain_42",
  "message": "今日の天気は？"
}
```

#### 🔊 응답

- `audio/wav` 형태의 스트리밍 음성
- 헤더에 GPT 텍스트 포함: `X-GPT-Reply`

---

### ❤️ `/analyze` (POST)

자체 감정 분석 API입니다. `text` 입력 → 감정 레이블 및 벡터 반환.

```json
POST /analyze
{
  "text": "なんか嬉しい！"
}
```

---

### 🔈 `/speak` (POST)

감정 벡터 기반으로 음성을 합성하는 TTS API입니다.

```json
POST /speak
{
  "text": "こんにちはにゃん！",
  "language": "ja",
  "emotions": [0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9, 0.0]
}
```

---

## 🛡️ 보안 기능

- ✅ **Prompt Injection 방어**
- ✅ **Rate Limiting**
- ✅ **Safe Eval**
- ✅ **Thread 관리**

---

## 📊 감정 벡터 구성

| Index | 감정       |
|-------|------------|
| 0     | 기쁨       |
| 1     | 슬픔       |
| 2     | 분노       |
| 3     | 두려움     |
| 4     | 놀라움     |
| 5     | 혐오       |
| 6     | 중립       |
| 7     | 기타       |

---

## 🩺 헬스체크

```http
GET /health
```

---

## 🧠 기술 스택

- FastAPI
- OpenAI GPT-4o (Tool Calling)
- transformers 감정 분석
- VITS 기반 TTS 서버
- `.env` 환경 구성

---

## 📁 프로젝트 구조

```
├── rene_app.py
├── tts_app.py
├── requirements.txt
├── 문제점분석.md
├── 공격프롬프트 정리.md
```

---

## 🐱 캐릭터 소개

**レネ (Rene)**  
- 말버릇: "にゃん"
- 말투: 짧고 캐주얼한 1문장
- 언어: 항상 일본어만 사용

---

## 📄 라이선스

MIT License © 2025 VOYAGER Inc.