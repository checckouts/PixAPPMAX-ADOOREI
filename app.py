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
from typing import Optional, Dict

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

# Tempo máximo (em segundos) que uma página pré-aquecida pode ficar no cache antes de ser descartada
PAGE_MAX_AGE_SECONDS = 120


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


class PreWarmedPage:
    """Armazena uma página pré-aquecida com timestamp de criação."""
    def __init__(self, page, created_at: float):
        self.page = page
        self.created_at = created_at

    def is_expired(self) -> bool:
        import time
        return (time.time() - self.created_at) > PAGE_MAX_AGE_SECONDS

    def is_valid(self) -> bool:
        return not self.page.is_closed() and not self.is_expired()


class BrowserManager:
    """
    Gerencia o Playwright com pool de páginas pré-aquecidas.
    Usa UM ÚNICO contexto para evitar crash do Chromium em ambientes limitados.
    Inclui auto-restart caso o browser caia.
    
    OTIMIZAÇÃO PRINCIPAL: Mantém páginas já carregadas com tokens prontos
    para uso imediato quando o usuário solicitar a geração do PIX.
    """

    def __init__(self, pool_size=3):
        self.playwright = None
        self.browser = None
        self.context = None
        self.pool_size = pool_size
        self._running = False
        self._starting = False
        self._lock = asyncio.Lock()

        # Pool de páginas pré-aquecidas: {checkout_url: [PreWarmedPage, ...]}
        self._warm_pages: Dict[str, list] = {}
        # Task de manutenção do pool
        self._maintenance_task = None

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
                    '--single-process',
                    '--disable-background-timer-throttling',
                    '--disable-renderer-backgrounding',
                ]
            )
            self.context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            )
            self._running = True
            logger.info("BrowserManager iniciado com sucesso")

            # Inicia o pré-aquecimento em background
            self._maintenance_task = asyncio.create_task(self._pool_maintenance_loop())

            # Pré-aquece as páginas dos subtotais mais comuns
            asyncio.create_task(self._initial_warmup())

        except Exception as e:
            logger.error(f"Erro ao iniciar BrowserManager: {e}")
        finally:
            self._starting = False

    async def _initial_warmup(self):
        """Pré-aquece páginas para os links mais usados no startup."""
        await asyncio.sleep(1)  # Espera o browser estabilizar

        # Pré-aquece o link padrão (mais usado)
        await self._add_warm_page(EXTERNAL_CHECKOUT_URL_DEFAULT)
        logger.info("Pré-aquecimento inicial concluído (link padrão)")

        # Pré-aquece os demais links em background (sem bloquear)
        for subtotal, url in SUBTOTAL_LINKS.items():
            if url != EXTERNAL_CHECKOUT_URL_DEFAULT:
                await self._add_warm_page(url)
                await asyncio.sleep(0.5)  # Espaça para não sobrecarregar
        
        logger.info(f"Pré-aquecimento completo: {len(self._warm_pages)} URLs prontas")

    async def _pool_maintenance_loop(self):
        """Loop de manutenção que remove páginas expiradas e repõe o pool."""
        while self._running:
            try:
                await asyncio.sleep(30)  # Verifica a cada 30 segundos
                await self._cleanup_expired_pages()
                await self._replenish_pool()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erro na manutenção do pool: {e}")
                await asyncio.sleep(5)

    async def _cleanup_expired_pages(self):
        """Remove páginas expiradas ou fechadas do pool."""
        for url in list(self._warm_pages.keys()):
            valid_pages = []
            for wp in self._warm_pages[url]:
                if wp.is_valid():
                    valid_pages.append(wp)
                else:
                    try:
                        if not wp.page.is_closed():
                            await wp.page.close()
                    except:
                        pass
                    logger.info(f"Página expirada removida para: {url}")
            self._warm_pages[url] = valid_pages

    async def _replenish_pool(self):
        """Garante que sempre haja pelo menos 1 página pronta para o link padrão."""
        default_url = EXTERNAL_CHECKOUT_URL_DEFAULT
        valid_count = sum(1 for wp in self._warm_pages.get(default_url, []) if wp.is_valid())

        if valid_count < 1:
            logger.info("Repondo página padrão no pool...")
            await self._add_warm_page(default_url)

    async def _add_warm_page(self, checkout_url: str):
        """Cria uma página pré-aquecida e adiciona ao pool."""
        try:
            page = await self._create_ready_page(checkout_url)
            if page:
                import time
                wp = PreWarmedPage(page, time.time())
                if checkout_url not in self._warm_pages:
                    self._warm_pages[checkout_url] = []
                self._warm_pages[checkout_url].append(wp)
                logger.info(f"Página pré-aquecida adicionada para: {checkout_url}")
        except Exception as e:
            logger.error(f"Erro ao pré-aquecer página para {checkout_url}: {e}")

    async def _restart(self):
        """Reinicia o browser caso ele tenha crashado."""
        logger.warning("Reiniciando browser...")
        self._running = False

        # Cancela a task de manutenção
        if self._maintenance_task:
            self._maintenance_task.cancel()

        # Limpa o pool
        for url, pages in self._warm_pages.items():
            for wp in pages:
                try:
                    if not wp.page.is_closed():
                        await wp.page.close()
                except:
                    pass
        self._warm_pages.clear()

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

    async def _create_ready_page(self, checkout_url: str):
        """Cria uma nova página já navegada para o checkout específico."""
        if not self.context:
            raise Exception("Contexto não disponível")

        page = await self.context.new_page()

        # Bloqueia recursos pesados de forma agressiva
        async def block_resources(route):
            resource_type = route.request.resource_type
            # Bloqueia tudo que não é essencial para obter o CSRF e cart_token
            if resource_type in ["image", "font", "media", "stylesheet"]:
                return await route.abort()
            url = route.request.url.lower()
            blocked_domains = [
                "facebook", "google-analytics", "hotjar", "clarity",
                "tiktok", "doubleclick", "gtag", "googletagmanager",
                "pixel", "analytics", "tracking", "adservice",
                "cdn.jsdelivr", "fonts.googleapis", "fonts.gstatic"
            ]
            if any(d in url for d in blocked_domains):
                return await route.abort()
            await route.continue_()

        await page.route("**/*", block_resources)

        # Navega para o checkout específico
        await page.goto(checkout_url, wait_until='domcontentloaded', timeout=20000)

        # Aguarda variáveis essenciais com timeout reduzido
        try:
            await page.wait_for_function(
                "window.ck && window.ck.data && window.ck.data.cart_token && document.querySelector('input[name=\"_token\"]')",
                timeout=12000
            )
            logger.info("Página carregada - CSRF e cart_token disponíveis")
        except Exception as e:
            logger.warning(f"Timeout aguardando variáveis: {e}")
            # Tenta esperar um pouco mais, mas sem bloquear demais
            await asyncio.sleep(1)

        return page

    async def get_ready_page(self, checkout_url: str):
        """
        MÉTODO PRINCIPAL: Retorna uma página pronta para uso.
        1. Tenta pegar do pool (instantâneo).
        2. Se não houver, cria uma nova (mais lento, mas funciona).
        3. Dispara reposição em background para o próximo usuário.
        """
        async with self._lock:
            # Tenta pegar uma página válida do pool
            if checkout_url in self._warm_pages:
                while self._warm_pages[checkout_url]:
                    wp = self._warm_pages[checkout_url].pop(0)
                    if wp.is_valid():
                        logger.info(f"Página pré-aquecida disponível para: {checkout_url}")
                        # Repõe em background para o próximo usuário
                        asyncio.create_task(self._add_warm_page(checkout_url))
                        return wp.page
                    else:
                        # Página expirada, fecha e tenta a próxima
                        try:
                            if not wp.page.is_closed():
                                await wp.page.close()
                        except:
                            pass

        # Se não encontrou no pool, cria uma nova (fallback)
        logger.info(f"Nenhuma página no pool para {checkout_url}, criando nova...")
        page = await self._create_ready_page(checkout_url)

        # Repõe em background
        asyncio.create_task(self._add_warm_page(checkout_url))

        return page

    async def close(self):
        """Encerra o browser e limpa todos os recursos."""
        self._running = False

        if self._maintenance_task:
            self._maintenance_task.cancel()

        # Fecha todas as páginas do pool
        for url, pages in self._warm_pages.items():
            for wp in pages:
                try:
                    if not wp.page.is_closed():
                        await wp.page.close()
                except:
                    pass
        self._warm_pages.clear()

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


browser_manager = BrowserManager(pool_size=3)


@app.on_event("startup")
async def startup_event():
    await browser_manager.start()


@app.on_event("shutdown")
async def shutdown_event():
    await browser_manager.close()


async def automate_pix_generation(data: PixRequest):
    """
    Gera PIX usando abordagem híbrida otimizada:
    - Usa página pré-aquecida do pool (instantâneo quando disponível)
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

    # Obtém página pronta do pool (ou cria uma nova se necessário)
    page = None
    try:
        page = await browser_manager.get_ready_page(checkout_url)
    except Exception as e:
        logger.error(f"Falha ao obter página: {e}")
        try:
            await browser_manager._restart()
            await asyncio.sleep(2)
            page = await browser_manager.get_ready_page(checkout_url)
        except Exception as e2:
            logger.error(f"Falha após restart: {e2}")
            return None, str(e2)

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
            await page.goto(checkout_url, wait_until='domcontentloaded', timeout=15000)
            await page.wait_for_function("window.form && typeof realizarPagamento === 'function'", timeout=8000)
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
            await asyncio.wait_for(response_received.wait(), timeout=8.0)
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
    """Health check com informações do pool."""
    pool_info = {}
    for url, pages in browser_manager._warm_pages.items():
        valid = sum(1 for wp in pages if wp.is_valid())
        pool_info[url] = valid

    return {
        "status": "ok",
        "browser_running": browser_manager._running,
        "warm_pages": pool_info
    }


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
