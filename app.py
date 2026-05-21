import os
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MAPEAMENTO DE SUBTOTAL → LINK DE CHECKOUT
# Adicione aqui os valores e seus respectivos links.
# A chave deve ser o valor do subtotal como string (ex: "89.90")
# O valor deve ser o link completo de checkout correspondente.
# ============================================================
SUBTOTAL_LINKS = {
    "89.90": "https://pay.meuservicomei.com.br/r/9D5r68181N8jQVa5Z4",
    "179.80": "https://pay.meuservicomei.com.br/r/87n4yU1280uM86T1M5",
    "269.70": "https://pay.meuservicomei.com.br/r/2Og12S5n8i16V1u8A8",
    "395.90": "https://pay.meuservicomei.com.br/r/vL06N28r2x815bz8",
    # Adicione quantos precisar...
}


# Link padrão caso o subtotal não seja encontrado no mapeamento
EXTERNAL_CHECKOUT_URL_DEFAULT = "https://pay.meuservicomei.com.br/r/9D5r68181N8jQVa5Z4"

EXTERNAL_BASE_URL = "https://pay.meuservicomei.com.br"


def get_checkout_url_by_subtotal(subtotal: Optional[str]) -> str:
    """
    Retorna o link de checkout correspondente ao subtotal informado.
    Se o subtotal não estiver mapeado ou não for informado, retorna o link padrão.
    """
    if subtotal and subtotal in SUBTOTAL_LINKS:
        url = SUBTOTAL_LINKS[subtotal]
        logger.info(f"Subtotal {subtotal} mapeado para: {url}")
        return url
    if subtotal:
        logger.warning(f"Subtotal {subtotal} não encontrado no mapeamento, usando link padrão")
    return EXTERNAL_CHECKOUT_URL_DEFAULT


class PixRequest(BaseModel):
    payer_name: str
    payer_cpf: str
    payer_phone: str
    payer_email: str = None
    subtotal: str = None  # Subtotal para determinar o link de checkout


class BrowserManager:
    """
    Gerencia o Playwright com pool de páginas pré-aquecidas.
    Usa UM ÚNICO contexto para evitar crash do Chromium em ambientes limitados.
    Inclui auto-restart caso o browser caia.
    """

    def __init__(self, pool_size=2):
        self.playwright = None
        self.browser = None
        self.context = None
        self.pool_size = pool_size
        self.page_queue: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._running = False
        self._starting = False

    async def start(self):
        """Inicia o Playwright e o browser."""
        if self._starting:
            return
        self._starting = True
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-default-apps',
                    '--disable-sync',
                    '--disable-translate',
                    '--metrics-recording-only',
                    '--no-first-run',
                ]
            )
            self.context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            )
            self._running = True
            logger.info("BrowserManager iniciado com sucesso")
        except Exception as e:
            logger.error(f"Erro ao iniciar BrowserManager: {e}")
        finally:
            self._starting = False

    async def _restart(self):
        """Reinicia o browser caso ele tenha crashado."""
        logger.warning("Reiniciando browser...")
        try:
            if self.context:
                await self.context.close()
        except:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except:
            pass

        self.playwright = None
        self.browser = None
        self.context = None

        await self.start()

    async def create_ready_page(self, checkout_url: str):
        """Cria uma nova página já navegada para o checkout específico."""
        if not self.context:
            raise Exception("Contexto não disponível")

        page = await self.context.new_page()

        # Bloqueia recursos pesados
        async def block_resources(route):
            if route.request.resource_type in ["image", "font", "media"]:
                return await route.abort()
            url = route.request.url.lower()
            if any(d in url for d in ["facebook", "google-analytics", "hotjar", "clarity", "tiktok", "doubleclick", "gtag"]):
                return await route.abort()
            await route.continue_()

        await page.route("**/*", block_resources)

        # Navega para o checkout específico
        await page.goto(checkout_url, wait_until='domcontentloaded', timeout=25000)

        # Aguarda variáveis essenciais
        try:
            await page.wait_for_function(
                "window.ck && window.ck.data && window.ck.data.cart_token && document.querySelector('input[name=\"_token\"]')",
                timeout=15000
            )
            logger.info("Página carregada - CSRF e cart_token disponíveis")
        except Exception as e:
            logger.warning(f"Timeout aguardando variáveis: {e}")
            await asyncio.sleep(2)

        return page

    async def close(self):
        self._running = False
        if self.context:
            try:
                await self.context.close()
            except:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except:
                pass


browser_manager = BrowserManager(pool_size=2)


@app.on_event("startup")
async def startup_event():
    await browser_manager.start()


@app.on_event("shutdown")
async def shutdown_event():
    await browser_manager.close()


async def automate_pix_generation(data: PixRequest):
    """
    Gera PIX usando abordagem híbrida:
    - Playwright para contornar Cloudflare
    - fetch() direto no JS para máxima velocidade
    - Fallback para realizarPagamento se necessário
    """
    # Determina o link de checkout com base no subtotal
    checkout_url = get_checkout_url_by_subtotal(data.subtotal)

    payer_email = data.payer_email
    if not payer_email:
        safe_name = ''.join(c for c in data.payer_name.lower() if c.isalpha() or c == ' ').replace(' ', '.')
        payer_email = f"{safe_name}@gmail.com"

    cpf_clean = ''.join(c for c in data.payer_cpf if c.isdigit())
    phone_clean = '11999999999'  # Telefone padrão fixo para todas as operações

    # Cria página diretamente com o checkout_url correto
    try:
        page = await browser_manager.create_ready_page(checkout_url)
    except Exception as e:
        logger.error(f"Falha ao criar página: {e}")
        await browser_manager._restart()
        await asyncio.sleep(2)
        page = await browser_manager.create_ready_page(checkout_url)

    try:
        # ===== MÉTODO 1: Fetch direto (mais rápido) =====
        logger.info("Tentando método rápido (fetch direto)...")

        result = await page.evaluate("""async (data) => {
            try {
                const csrfEl = document.querySelector('input[name="_token"]');
                if (!csrfEl) return { success: false, error: 'CSRF token não encontrado' };

                const csrf = csrfEl.value;
                const cartToken = window.ck && window.ck.data ? window.ck.data.cart_token : null;
                if (!cartToken) return { success: false, error: 'cart_token não encontrado' };

                const payload = {
                    inputs_with_errors: [],
                    cart_token: cartToken,
                    payment_method: 'pix_appmax',
                    email: data.email,
                    first_name: data.name,
                    doc: data.cpf,
                    phone: data.phone,
                    postal_code: '01310100',
                    address_line_1: 'Avenida Paulista',
                    address_number: '1000',
                    address_neighborhood: 'Bela Vista',
                    city: 'São Paulo',
                    state: 'SP',
                    address_disabled: 1,
                    opt_in: true,
                    is_province: false,
                    card_installments: '1'
                };

                const response = await fetch('/orders', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-CSRF-TOKEN': csrf
                    },
                    body: JSON.stringify(payload)
                });

                const json = await response.json();
                return { success: true, status: response.status, data: json };
            } catch (e) {
                return { success: false, error: e.toString() };
            }
        }""", {
            'email': payer_email,
            'name': data.payer_name,
            'cpf': cpf_clean,
            'phone': phone_clean
        })

        logger.info(f"Resultado fetch direto: {result}")

        if result and result.get('success') and result.get('data'):
            resp_data = result['data']

            if resp_data.get('redirect'):
                redirect = resp_data['redirect']
                pix_url = redirect if redirect.startswith('http') else f"{EXTERNAL_BASE_URL}/{redirect.lstrip('/')}"
                logger.info(f"PIX gerado (método rápido): {pix_url}")
                return pix_url, None
            elif resp_data.get('url'):
                return resp_data['url'], None
            elif resp_data.get('errors'):
                errors = resp_data['errors']
                first_key = list(errors.keys())[0]
                first_error = errors[first_key]
                error_msg = first_error[0] if isinstance(first_error, list) else str(first_error)
                logger.warning(f"Erro da API (método rápido): {error_msg}")
                # Não retorna erro aqui, tenta o fallback
            else:
                logger.warning(f"Resposta inesperada: {resp_data}")

        # ===== MÉTODO 2: Fallback via realizarPagamento =====
        error_from_method1 = result.get('error') if result else 'resultado vazio'
        logger.info(f"Método rápido não retornou URL ({error_from_method1}), tentando fallback...")

        pix_url = None
        error_msg = None
        response_received = asyncio.Event()

        async def handle_response(response):
            nonlocal pix_url, error_msg
            url = response.url
            if response.status < 400 and ('/orders' in url or '/pagamento' in url):
                try:
                    resp_data = await response.json()
                    if resp_data.get('redirect'):
                        redirect = resp_data['redirect']
                        pix_url = redirect if redirect.startswith('http') else f"{EXTERNAL_BASE_URL}/{redirect.lstrip('/')}"
                        response_received.set()
                    elif resp_data.get('url'):
                        pix_url = resp_data['url']
                        response_received.set()
                    elif resp_data.get('errors'):
                        errors = resp_data['errors']
                        first_key = list(errors.keys())[0]
                        first_error = errors[first_key]
                        error_msg = first_error[0] if isinstance(first_error, list) else str(first_error)
                        response_received.set()
                except:
                    pass

        page.on('response', handle_response)

        # Recarrega para estado limpo usando o checkout_url correto
        try:
            await page.goto(checkout_url, wait_until='domcontentloaded', timeout=20000)
            await page.wait_for_function("window.form && typeof realizarPagamento === 'function'", timeout=10000)
        except Exception as e:
            logger.warning(f"Erro no reload do fallback: {e}")

        try:
            await page.evaluate("""(data) => {
                window.form.email = data.email;
                window.form.first_name = data.name;
                window.form.doc = data.cpf;
                window.form.phone = data.phone;
                window.form.postal_code = '01310100';
                window.form.address_line_1 = 'Avenida Paulista';
                window.form.address_number = '1000';
                window.form.address_neighborhood = 'Bela Vista';
                window.form.city = 'São Paulo';
                window.form.state = 'SP';
                window.form.inputs_with_errors = [];
                window.form.address_disabled = 1;
                window.form.payment_method = 'pix_appmax';

                const btn = document.querySelector('#general-submit-button') || document.createElement('button');
                btn.disabled = false;
                realizarPagamento(btn);
            }""", {
                'email': payer_email,
                'name': data.payer_name,
                'cpf': cpf_clean,
                'phone': phone_clean
            })
        except Exception as e:
            logger.error(f"Erro ao executar realizarPagamento: {e}")
            return None, str(e)

        try:
            await asyncio.wait_for(response_received.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            current_url = page.url
            if any(kw in current_url for kw in ['obrigado', 'sucesso', 'pix']):
                pix_url = current_url

        if pix_url:
            logger.info(f"PIX gerado (fallback): {pix_url}")
        else:
            logger.error(f"Falha em ambos os métodos. Erro: {error_msg}")

        return pix_url, error_msg

    except Exception as e:
        logger.error(f"Erro geral: {e}", exc_info=True)
        return None, str(e)
    finally:
        try:
            await page.close()
        except:
            pass


@app.post('/proxy/pix')
async def proxy_pix(request: PixRequest):
    logger.info(f"Requisição: {request.payer_name} / {request.payer_cpf} / subtotal: {request.subtotal}")
    pix_url, error = await automate_pix_generation(request)
    if pix_url:
        return JSONResponse({'success': True, 'pixUrl': pix_url, 'redirectUrl': pix_url})
    return JSONResponse(
        {'success': False, 'error': error or 'Erro ao gerar PIX', 'message': 'Não foi possível gerar o PIX. Tente novamente.'},
        status_code=400
    )


@app.get('/health')
async def health():
    return {"status": "ok"}


@app.get('/')
async def index():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')


# Montar arquivos estáticos
static_dir = Path(__file__).parent / 'static'
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
