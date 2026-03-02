## 1. System Requirements

The project relies on several system-level tools for file conversion, PDF processing, and audio transcription.

* **LibreOffice**: To convert PPT/PPTX slides to PDF.
* **Poppler-utils**: For `pdf2image` to extract frames from PDF files.
* **wkhtmltopdf**: Used by `pdfkit` to generate PDF reports from Markdown.
* **FFmpeg**: Required for audio processing and STT (Faster-Whisper).

---

## 2. Option A: Local Installation

### 1) Install System Packages

**Ubuntu / Debian:**

```bash
sudo apt update
sudo apt install -y libreoffice poppler-utils wkhtmltopdf ffmpeg fonts-nanum

```

### 2) Setup Python Environment

```bash
# Clone the repository
git clone https://github.com/yojun313/LecAI.git
cd LecAI

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

```

### 3) Configuration

Create a `.env` file in the root directory. There exists `.env.example` in root directory.

### 4) Run the Server

```bash
python main.py
# Or using uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

```