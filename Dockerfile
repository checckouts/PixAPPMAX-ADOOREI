# Use a imagem oficial do Python com Playwright pré-instalado
FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

# Definir diretório de trabalho
WORKDIR /app

# Copiar arquivos de requisitos e instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar o restante do código
COPY . .

# O Playwright já vem com os navegadores na imagem da Microsoft
RUN playwright install chromium

# Expor a porta
EXPOSE 5000

# Aumentar shared memory para o Chromium (evita crashes)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Comando para iniciar
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000", "--timeout-keep-alive", "60"]
