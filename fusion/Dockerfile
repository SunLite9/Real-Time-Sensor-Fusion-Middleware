FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fusion/ekf.py fusion/fusion_service.py fusion/timing.py fusion/fault_events.py ./
EXPOSE 8080
CMD ["python", "fusion_service.py"]
