import os
import shutil
import base64
import requests
import subprocess
import zipfile
import pdfkit
import threading
import concurrent.futures
import time 
from pdf2image import convert_from_path
from app.core.config import settings
from app.services.job_manager import JobManager
from app.services.auth_manager import AuthManager
from app.db.prompt import default_system_prompt, default_user_prompt

# ==========================================
# Global Lock
# ==========================================
# Local LLM 사용 시에만 작동할 Lock (GPU 자원 보호)
local_gpu_lock = threading.Lock()

PRICING_TABLE = {
    "gpt-5.2": {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5-mini": {"input": 0.25, "cached": 0.025, "output": 2.00},
    "gpt-4o": {"input": 2.50, "cached": 1.25, "output": 10.00},
}

# ==========================================
# Helper Functions
# ==========================================

def get_headers(api_key=None):
    if api_key:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {settings.CUSTOM_TOKEN}", "Content-Type": "application/json"}

def get_target_model(user_settings):
    pref = user_settings.get("preferred_model", "local")
    user_key = user_settings.get("openai_api_key", "")
    
    system_prompt = user_settings.get("custom_prompt", "") 
    user_prompt_template = user_settings.get("custom_user_prompt", "")

    config = {
        "model_id": "gpt-4o", 
        "base_url": settings.CUSTOM_BASE_URL,
        "api_key": None,
        "provider": "local",
        "system_prompt": system_prompt,
        "user_prompt_template": user_prompt_template
    }

    if pref.startswith("gpt"):
        if not user_key:
            raise ValueError("OpenAI 모델이 선택되었으나 API Key가 설정되지 않았습니다.")
        config.update({
            "provider": "openai",
            "model_id": pref,
            "base_url": "https://api.openai.com/v1",
            "api_key": user_key
        })
    else:
        try:
            url = f"{settings.CUSTOM_BASE_URL}/models"
            resp = requests.get(url, headers=get_headers(), timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                if models:
                    config["model_id"] = models[0]["id"]
        except:
            pass

    return config

def describe_image(image_path: str, model_config: dict):
    filename = os.path.basename(image_path)
    url = f"{model_config['base_url']}/chat/completions"
    headers = get_headers(model_config['api_key'])

    def image_to_data_url(path):
        with open(path, "rb") as f:
            raw = f.read()
        encoded = base64.b64encode(raw).decode("utf-8")
        ext = os.path.splitext(path)[1].lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        return f"data:{mime};base64,{encoded}"
   
    system_instruction = model_config.get("system_prompt", default_system_prompt)
    if not system_instruction.strip():
        system_instruction = default_system_prompt
    
    user_template = model_config.get("user_prompt_template", "")
    if not user_template or not user_template.strip():
        user_template = default_user_prompt
        
    user_instruction = user_template.replace("{filename}", filename)

    payload = {
        "model": model_config['model_id'],
        "messages": [
            {"role": "system", "content": system_instruction.strip()},
            {"role": "user", "content": [
                {"type": "text", "text": user_instruction.strip()},
                {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}}
            ]}
        ],
        "max_completion_tokens": 3000,
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=180)
            
            if resp.status_code == 200:
                result = resp.json()
                content = result["choices"][0]["message"].get("content", "")
                
                if not content or not content.strip():
                    print(f"[Empty Response] {filename} returned empty content. Retrying... ({attempt+1}/{max_retries})")
                    time.sleep(2)
                    continue

                usage_info = {"prompt": 0, "cached": 0, "completion": 0}
                try:
                    usage = result.get('usage', {})
                    usage_info["prompt"] = usage.get('prompt_tokens', 0)
                    usage_info["completion"] = usage.get('completion_tokens', 0)
                    usage_info["cached"] = usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
                except:
                    pass
                return content, usage_info

            elif resp.status_code == 429:
                wait_time = (attempt + 1) * 5
                print(f"[Rate Limit] 429 Error on {filename}. Waiting {wait_time}s... (Attempt {attempt+1}/{max_retries})")
                time.sleep(wait_time)
                continue
            
            else:
                print(f"[API Error] {resp.status_code}: {resp.text}")
                if resp.status_code >= 500:
                    time.sleep(3)
                    continue
                raise RuntimeError(f"OpenAI API Error: {resp.status_code} - {resp.text}")

        except requests.exceptions.Timeout:
            print(f"[Timeout] {filename} timed out. Retrying...")
            time.sleep(3)
            continue
            
        except Exception as e:
            print(f"[Exception] {str(e)}")
            if attempt == max_retries - 1:
                raise e
            time.sleep(2)

    raise RuntimeError(f"Failed to process {filename} after {max_retries} attempts.")

def convert_ppt_to_pdf(ppt_path: str, output_dir: str):
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", output_dir, ppt_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    base = os.path.splitext(os.path.basename(ppt_path))[0]
    return os.path.join(output_dir, f"{base}.pdf")

def calculate_total_cost(model_id, total_usage, exchange_rate=1400):
    matched_model = next((m for m in PRICING_TABLE if m in model_id), "default")
    if matched_model == "default": return 0.0, 0
    
    rates = PRICING_TABLE[matched_model]
    regular_input = total_usage['prompt'] - total_usage['cached']
    
    usd_cost = (
        (regular_input * rates['input']) +
        (total_usage['cached'] * rates['cached']) +
        (total_usage['completion'] * rates['output'])
    ) / 1000000
    
    return round(usd_cost, 4), int(usd_cost * exchange_rate)

# ==========================================
# Main Processing Logic
# ==========================================

def _process_job_internal(job_id: str, file_path: str, model_config: dict, owner: str):
    """
    실제 파일 처리 로직 (실시간 비용 로그 추가)
    """
    work_dir = os.path.join(settings.UPLOAD_DIR, job_id)
    os.makedirs(work_dir, exist_ok=True)
    
    try:
        JobManager.start_processing(job_id)
        
        # 1. 이미지 변환 (PDF/PPT -> Images)
        images = []
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == ".pdf":
            raw_images = convert_from_path(file_path, fmt="png", dpi=150)
            for i, img in enumerate(raw_images):
                p = os.path.join(work_dir, f"page_{i+1:03d}.png"); img.save(p); images.append(p)
        elif ext in [".ppt", ".pptx"]:
            pdf_path = convert_ppt_to_pdf(file_path, work_dir)
            raw_images = convert_from_path(pdf_path, fmt="png", dpi=150)
            for i, img in enumerate(raw_images):
                p = os.path.join(work_dir, f"page_{i+1:03d}.png"); img.save(p); images.append(p)

        result_base = os.path.join(settings.RESULT_DIR, job_id)
        result_images_dir = os.path.join(result_base, "images")
        os.makedirs(result_images_dir, exist_ok=True)

        total_pages = len(images)
        cumulative_usage = {"prompt": 0, "cached": 0, "completion": 0}
        results_map = {} 
        
        # 2. LLM 분석 (병렬 vs 순차)
        if model_config['provider'] == 'openai':
            max_workers = 3
            completed_count = 0
            progress_lock = threading.Lock()

            def process_single_slide(idx, img_path):
                img_filename = os.path.basename(img_path)
                shutil.copy2(img_path, os.path.join(result_images_dir, img_filename))
                content, usage = describe_image(img_path, model_config)
                return idx, content, usage, img_filename

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
                    executor.submit(process_single_slide, idx, img_path): idx 
                    for idx, img_path in enumerate(images, 1)
                }

                for future in concurrent.futures.as_completed(future_to_idx):
                    try:
                        idx, content, usage, filename = future.result()
                        results_map[idx] = (filename, content)
                        
                        with progress_lock:
                            # 사용량 누적
                            for k in cumulative_usage:
                                cumulative_usage[k] += usage[k]
                            completed_count += 1
                            
                            # 실시간 비용 계산
                            usd_val, krw_val = calculate_total_cost(model_config['model_id'], cumulative_usage)
                            cur_tokens = cumulative_usage['prompt'] + cumulative_usage['completion']
                            
                            # 로그 메시지에 비용 정보 포함
                            log_msg = (
                                f"분석 중 ({completed_count}/{total_pages}) | "
                                f"누적 토큰: {cur_tokens:,} | "
                                f"예상 비용: ${usd_val:.3f} (₩{krw_val:,})"
                            )
                            
                            JobManager.update_progress(
                                job_id, completed_count, total_pages, log_msg
                            )
                            
                    except Exception as e:
                        print(f"[FINAL ERROR] Slide processing failed: {e}")
                        failed_idx = future_to_idx[future]
                        results_map[failed_idx] = ("error.png", f"**[분석 실패]** 오류가 발생했습니다: {str(e)}")

        else:
            # Local LLM: 순차 처리
            for idx, img_path in enumerate(images, 1):
                img_filename = os.path.basename(img_path)
                shutil.copy2(img_path, os.path.join(result_images_dir, img_filename))
                
                content, usage = describe_image(img_path, model_config)
                
                # 사용량 누적
                for k in cumulative_usage:
                    cumulative_usage[k] += usage[k]
                
                results_map[idx] = (img_filename, content)

                usd_val, krw_val = calculate_total_cost(model_config['model_id'], cumulative_usage)
                cur_tokens = cumulative_usage['prompt'] + cumulative_usage['completion']
                
                log_msg = (
                    f"분석 중 ({idx}/{total_pages}) | "
                    f"누적 토큰: {cur_tokens:,}"
                )

                JobManager.update_progress(job_id, idx, total_pages, log_msg)

        # 3. 결과 조합 (인덱스 순서대로)
        md_content = ""
        sorted_indices = sorted(results_map.keys())
        for idx in sorted_indices:
            fname, text = results_map[idx]
            md_content += f"## Slide {idx}\n\n![{fname}](./images/{fname})\n\n{text}\n\n---\n\n"

        # 4. 최종 완료 처리
        if model_config['provider'] == 'openai':
            usd_val, krw_val = calculate_total_cost(model_config['model_id'], cumulative_usage)
            job = JobManager.get_job(job_id)
            if job:
                AuthManager.update_user_cumulative_usage(job['owner'], usd_val)
            final_log = f"작업 완료! 총 비용: ${usd_val} (약 ₩{krw_val:,}) | 총 토큰: {cumulative_usage['prompt'] + cumulative_usage['completion']}"
        else:
            final_log = f"작업 완료! 총 토큰: {cumulative_usage['prompt'] + cumulative_usage['completion']}"
        JobManager.update_progress(job_id, total_pages, total_pages, final_log)
        
        # Markdown 저장
        md_file = os.path.join(result_base, "result.md")
        with open(md_file, "w", encoding="utf-8") as f: f.write(md_content)
        
        # PDF 생성
        import markdown
        raw_html = markdown.markdown(md_content)
        
        # 절대 경로 변환 (wkhtmltopdf 에러 방지)
        abs_image_dir = os.path.abspath(os.path.join(result_base, "images")).replace("\\", "/")
        pdf_html_body = raw_html.replace('./images', f'file://{abs_image_dir}')

        full_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: sans-serif; padding: 20px; line-height: 1.6; }}
                img {{ max-width: 100%; height: auto; display: block; margin: 20px auto; border: 1px solid #ddd; }}
                h2 {{ border-bottom: 2px solid #333; padding-bottom: 10px; margin-top: 30px; page-break-before: always; }}
                h2:first-of-type {{ page-break-before: auto; }}
                blockquote {{ background: #f9f9f9; border-left: 10px solid #ccc; margin: 1.5em 10px; padding: 0.5em 10px; }}
            </style>
        </head>
        <body>
            {pdf_html_body}
        </body>
        </html>
        """

        pdf_options = {
            "quiet": "",
            "enable-local-file-access": "", # 필수
            "encoding": "UTF-8",
            "no-outline": None
        }

        pdfkit.from_string(
            full_html, 
            os.path.join(result_base, "result.pdf"), 
            options=pdf_options
        )
        
        # 압축 및 정리
        user_result_dir = os.path.join(settings.RESULT_DIR, owner)
        os.makedirs(user_result_dir, exist_ok=True)
        shutil.make_archive(os.path.join(user_result_dir, job_id), 'zip', result_base)
        
        # [Cleanup] 압축 후 원본 폴더 삭제
        if os.path.exists(result_base):
            shutil.rmtree(result_base)
            
        JobManager.mark_completed(job_id, f"/static/results/{owner}/{job_id}.zip")

    except Exception as e:
        JobManager.mark_failed(job_id, str(e))
    finally:
        # [Cleanup] 임시 작업 폴더 및 업로드 원본 삭제
        if os.path.exists(work_dir): shutil.rmtree(work_dir)
        if os.path.exists(file_path): os.remove(file_path)
        

def process_file_task(job_id: str, file_path: str):
    """
    Celery나 BackgroundTasks에서 호출되는 진입점
    """
    job = JobManager.get_job(job_id)
    if not job: return
    
    owner = job.get("owner")
    user_settings = AuthManager.get_user_settings(owner)
    
    try:
        model_config = get_target_model(user_settings)
    except Exception as e:
        JobManager.mark_failed(job_id, f"설정 오류: {str(e)}")
        return

    # 모델 타입에 따라 Lock 사용 여부 결정
    if model_config['provider'] == 'local':
        print(f"[Queue] Job {job_id} is waiting for GPU lock...")
        with local_gpu_lock:
            _process_job_internal(job_id, file_path, model_config, owner)
    else:
        print(f"[Queue] Job {job_id} is starting immediately (API Mode).")
        _process_job_internal(job_id, file_path, model_config, owner)