# app/services/audio_processor.py

import os
import json
import requests
import shutil
import threading
from app.core.config import settings
from app.services.job_manager import JobManager
from app.services.auth_manager import AuthManager

audio_lock = threading.Lock()

def process_audio_task(job_id: str, file_path: str):
    # 1. 사용자 설정 로드
    job = JobManager.get_job(job_id)
    if not job:
        return
    
    owner = job.get("owner")
    user_settings = AuthManager.get_user_settings(owner)
    
    # 설정값 가져오기
    language = user_settings.get("audio_language", "auto")
    model_level = user_settings.get("audio_model_level", 2)
    
    # 대기열 로깅
    try:
        queue_pos = JobManager.get_queue_position(job_id)
        if queue_pos > 0:
            JobManager.update_progress(job_id, 0, 0, f"대기열 진입: 앞선 작업 {queue_pos}개 대기 중")
        else:
            JobManager.update_progress(job_id, 0, 0, "오디오 변환 준비 중...")
    except: pass

    with audio_lock:
        try:
            JobManager.start_processing(job_id)
            JobManager.update_progress(job_id, 0, 0, "오디오 서버로 전송 시작...")

            # API 호출 준비
            headers = {}
            if settings.CUSTOM_TOKEN:
                headers["Authorization"] = f"Bearer {settings.CUSTOM_TOKEN}"

            fname = os.path.basename(file_path)
            
            # API 요청
            with open(file_path, "rb") as f:
                files_payload = {
                    "file": (fname, f, "audio/mpeg") # MIME 타입은 상황에 맞게
                }
                
                # 옵션 설정
                payload = {
                    "option": json.dumps({
                        "language": language,
                        "model": model_level,
                        "pid": None
                    })
                }

                JobManager.update_progress(job_id, 10, 100, "서버 변환 처리 중 (시간이 걸릴 수 있습니다)...")
                
                response = requests.post(
                    settings.AUDIO_LLM_URL,
                    headers=headers,
                    files=files_payload,
                    data=payload,
                    timeout=3600, # 1시간 타임아웃
                )

            if response.status_code != 200:
                raise RuntimeError(f"API Error: {response.status_code} - {response.text}")

            result = response.json()
            
            # 결과 처리
            result_base = os.path.join(settings.RESULT_DIR, job_id)
            os.makedirs(result_base, exist_ok=True)
            
            base_name = os.path.splitext(fname)[0]
            
            # 텍스트 파일 저장 (일반 텍스트)
            txt_path = os.path.join(result_base, f"{base_name}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(result.get("text", ""))

            # 타임스탬프 파일 저장
            time_path = os.path.join(result_base, f"{base_name}_timestamps.txt")
            with open(time_path, "w", encoding="utf-8") as f:
                f.write(result.get("text_with_time", ""))

            JobManager.update_progress(job_id, 90, 100, "결과물 압축 중...")
            user_result_dir = os.path.join(settings.RESULT_DIR, owner)
            os.makedirs(user_result_dir, exist_ok=True)
           
            shutil.make_archive(os.path.join(user_result_dir, job_id), 'zip', result_1base)
                
            JobManager.mark_completed(job_id, f"/static/results/{owner}/{job_id}.zip")

        except Exception as e:
            print(f"[AUDIO ERROR] {e}")
            JobManager.mark_failed(job_id, str(e))
        finally:
            # 원본 임시 파일 삭제
            if os.path.exists(file_path):
                os.remove(file_path)
            # 임시 결과 폴더 삭제
            result_dir = os.path.join(settings.RESULT_DIR, job_id)
            if os.path.exists(result_dir):
                shutil.rmtree(result_dir)