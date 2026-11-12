#!/usr/bin/env python3
"""Monitor gratuito de ofertas. Python 3.10+; instruções completas no README."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import re
import tempfile
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
import requests


LOG = logging.getLogger("arbitragem")
CENTAVO = Decimal("0.01")
TAXA = Decimal("0.16")
# A identidade HTTP acompanha a versão de Chrome usada no handshake TLS.
NAVEGADORES = [
    ("chrome120", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    ("chrome124", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("chrome131", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
]
DOMINIOS = {
    "amazon.com.br": "amazon",
    "mercadolivre.com.br": "mercadolivre",
    "mercadolibre.com.br": "mercadolivre",
    "kabum.com.br": "kabum",
}


class ErroMonitor(Exception):
    """Falha recuperável de um produto, sem interromper os demais."""


class Bloqueio(ErroMonitor):
    def __init__(self, mensagem, espera=900):
        super().__init__(mensagem)
        self.espera = espera


def dinheiro(valor) -> Decimal:
    """Normaliza BRL e números de JSON-LD sem confundir milhar e decimal."""
    if isinstance(valor, bool):
        raise ValueError("Preço inválido")
    if isinstance(valor, (int, float, Decimal)):
        numero = Decimal(str(valor))
        if not numero.is_finite() or numero <= 0:
            raise ValueError("Preço deve ser positivo")
        numero = numero.quantize(CENTAVO, rounding=ROUND_HALF_UP)
        if numero <= 0:
            raise ValueError("Preço menor que um centavo")
        return numero
    texto = re.sub(r"[\s\u00a0]", "", str(valor)).replace("R$", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", texto):
        raise ValueError("Preço inválido")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", texto):
        texto = texto.replace(".", "")
    try:
        numero = Decimal(texto)
    except InvalidOperation as exc:
        raise ValueError("Preço inválido") from exc
    if not numero.is_finite() or numero <= 0:
        raise ValueError("Preço deve ser positivo")
    numero = numero.quantize(CENTAVO, rounding=ROUND_HALF_UP)
    if numero <= 0:
        raise ValueError("Preço menor que um centavo")
    return numero


def loja_da_url(url: str) -> str:
    partes = urlsplit(url)
    host = (partes.hostname or "").lower()
    if partes.scheme != "https" or partes.username or partes.password or partes.port not in (None, 443):
        raise ValueError("Use um link HTTPS direto da loja, sem credenciais")
    for dominio, loja in DOMINIOS.items():
        if host == dominio or host.endswith("." + dominio):
            return loja
    raise ValueError("Loja não suportada: use Amazon Brasil, Mercado Livre ou Kabum")


def carregar_produtos(caminho: Path) -> list[dict]:
    dados = json.loads(caminho.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(dados, list) or not dados:
        raise ValueError("produtos.json deve conter uma lista não vazia")
    produtos, urls = [], set()
    for indice, item in enumerate(dados, 1):
        try:
            if not isinstance(item, dict) or not isinstance(item.get("nome"), str) or not item["nome"].strip():
                raise ValueError("Nome ausente")
            produto = {**item, "loja": loja_da_url(item["url"])}
            for chave in ("preco_alvo", "preco_revenda_estimado"):
                produto[chave] = dinheiro(item[chave])
            if produto["url"] in urls:
                raise ValueError("URL duplicada")
            urls.add(produto["url"])
            produtos.append(produto)
        except (ValueError, TypeError, KeyError) as exc:
            LOG.error("Produto %s ignorado: %s", indice, exc)
    if not produtos:
        raise ValueError("Nenhum produto válido")
    return produtos


def extrair_preco(html: str, loja: str) -> Decimal:
    soup = BeautifulSoup(html, "html.parser")
    # Seletor específico evita capturar parcelas, preços riscados e recomendações.
    seletores = {
        "amazon": [
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "#corePrice_feature_div .priceToPay .a-offscreen",
            "#apex_desktop .priceToPay .a-offscreen",
            "#priceblock_ourprice", "#priceblock_dealprice",
        ],
        "mercadolivre": [
            ".ui-pdp-price__second-line .andes-money-amount:not(.andes-money-amount--previous)",
        ],
        "kabum": ["#blocoValores #valorVista", "#valorVista", "[data-testid='price-pix']"],
    }
    for seletor in seletores[loja]:
        elemento = soup.select_one(seletor)
        if elemento is None:
            continue
        fracao = elemento.select_one(".andes-money-amount__fraction")
        if fracao:
            centavos = elemento.select_one(".andes-money-amount__cents")
            texto = fracao.get_text(strip=True) + "," + (centavos.get_text(strip=True) if centavos else "00")
        else:
            texto = elemento.get_text(" ", strip=True)
        try:
            return dinheiro(texto)
        except ValueError:
            continue

    # Fallback restrito ao Product principal e sua oferta. Nunca busca qualquer
    # campo "price" no HTML: isso poderia selecionar outro produto ou parcela.
    def produtos_ld(no):
        if isinstance(no, list):
            for filho in no:
                yield from produtos_ld(filho)
        elif isinstance(no, dict):
            tipos = no.get("@type", [])
            if isinstance(tipos, str):
                tipos = [tipos]
            if "Product" in tipos:
                yield no
            if "@graph" in no:
                yield from produtos_ld(no["@graph"])

    candidatos = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            candidatos.extend(produtos_ld(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    if len(candidatos) == 1:
        ofertas = candidatos[0].get("offers", [])
        ofertas = ofertas if isinstance(ofertas, list) else [ofertas]
        precos = set()
        for oferta in ofertas:
            if not isinstance(oferta, dict) or oferta.get("priceCurrency") != "BRL":
                continue
            disponibilidade = oferta.get("availability", "")
            if disponibilidade and disponibilidade.rsplit("/", 1)[-1] not in ("InStock", "LimitedAvailability"):
                continue
            try:
                precos.add(dinheiro(oferta["price"]))
            except (KeyError, ValueError, TypeError):
                continue
        if len(precos) == 1:
            return precos.pop()
    raise ErroMonitor("Preço não identificado com segurança; verifique estoque, seletor ou página de bloqueio")


def espera_retry_after(valor: str | None) -> float:
    try:
        segundos = float(valor)
        if not math.isfinite(segundos):
            raise ValueError
    except (TypeError, ValueError):
        try:
            data = parsedate_to_datetime(valor)
            segundos = data.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            segundos = 900
    return max(900, segundos)


def consultar_preco(sessao, produto: dict) -> Decimal:
    perfil, agente = random.choice(NAVEGADORES)
    # Redirecionamentos são seguidos manualmente para permanecer nas lojas permitidas.
    url = produto["url"]
    for _ in range(4):
        resposta = sessao.get(
            url, impersonate=perfil, timeout=30, allow_redirects=False,
            verify=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True,
            headers={"User-Agent": agente, "Accept-Language": "pt-BR,pt;q=0.9", "Accept": "text/html,application/xhtml+xml"},
        )
        if resposta.status_code in (301, 302, 303, 307, 308):
            from urllib.parse import urljoin
            url = urljoin(url, resposta.headers.get("Location", ""))
            if loja_da_url(url) != produto["loja"]:
                raise ErroMonitor("Redirecionamento para outra loja")
            time.sleep(random.uniform(5, 15))
            continue
        if resposta.status_code in (403, 429, 503):
            raise Bloqueio(f"HTTP {resposta.status_code}; loja em pausa", espera_retry_after(resposta.headers.get("Retry-After")))
        if resposta.status_code != 200:
            raise ErroMonitor(f"HTTP {resposta.status_code}")
        if "html" not in resposta.headers.get("Content-Type", "").lower():
            raise ErroMonitor("Resposta não é HTML")
        texto = resposta.text
        if any(sinal in texto.lower() for sinal in ("/errors/validatecaptcha", "cf-chl-", "verify you are human", "captcha-container", "robot check")):
            raise Bloqueio("CAPTCHA/desafio detectado; loja em pausa")
        return extrair_preco(texto, produto["loja"])
    raise ErroMonitor("Redirecionamentos em excesso")


def carregar_historico(caminho: Path) -> dict:
    if not caminho.exists():
        return {}
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    if not isinstance(dados, dict):
        raise ValueError("Histórico inválido; restaure um backup antes de executar")
    for url, entrada in dados.items():
        if not isinstance(entrada, dict):
            raise ValueError("Entrada inválida no histórico")
        dinheiro(entrada["preco"])
    return dados


def salvar_historico(caminho: Path, dados: dict):
    """Substituição atômica: uma interrupção não deixa um JSON pela metade."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=caminho.parent, prefix=caminho.name + ".", suffix=".tmp", delete=False) as arquivo:
            temporario = Path(arquivo.name)
            json.dump(dados, arquivo, ensure_ascii=False, indent=2)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, caminho)
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)


@contextmanager
def instancia_unica(caminho: Path):
    """Lock liberado pelo Linux ao sair, inclusive em caso de interrupção."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("a") as arquivo:
        try:
            fcntl.flock(arquivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ErroMonitor("Outro monitor já usa este histórico") from exc
        try:
            yield
        finally:
            fcntl.flock(arquivo, fcntl.LOCK_UN)


def calcular_lucro(preco, revenda):
    lucro = (revenda * (1 - TAXA) - preco).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    roi = (lucro / preco * 100).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    return lucro, roi


def deve_alertar(produto, preco, historico):
    lucro, _ = calcular_lucro(preco, produto["preco_revenda_estimado"])
    anterior = historico.get(produto["url"])
    return preco <= produto["preco_alvo"] and lucro > 0 and (anterior is None or preco < dinheiro(anterior["preco"]))


def brl(valor):
    return "R$ " + f"{valor:,.2f}".replace(",", "_CHAR_").replace(".", ",").replace("_CHAR_", ".")


def markdown(texto):
    # Telegram MarkdownV2 exige escapar todos estes caracteres fora de links.
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(texto))


def mensagem_oferta(produto, preco):
    lucro, roi = calcular_lucro(preco, produto["preco_revenda_estimado"])
    link = produto["url"].replace("\\", "\\\\").replace(")", "\\)")
    return (
        f"📉 *Oferta para revenda*\n\n*{markdown(produto['nome'])}*\n"
        f"Preço atual: *{markdown(brl(preco))}*\n"
        f"Revenda estimada: {markdown(brl(produto['preco_revenda_estimado']))}\n"
        f"Lucro estimado após taxa de 16%: *{markdown(brl(lucro))}*\n"
        f"ROI estimado: *{markdown(str(roi).replace('.', ',') + '%')}*\n\n"
        f"[Abrir produto]({link})\n\n"
        "Estimativa sem frete, impostos, embalagem ou garantia de venda\\."
    )


class Telegram:
    def __init__(self, token, chat_id):
        self.endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.sessao = requests.Session()

    def enviar(self, texto):
        try:
            resposta = self.sessao.post(self.endpoint, json={
                "chat_id": self.chat_id, "text": texto, "parse_mode": "MarkdownV2",
                "link_preview_options": {"is_disabled": True},
            }, timeout=(10, 30))
        except requests.RequestException:
            # Exceções de requests podem incluir o token na URL: nunca as imprima.
            raise ErroMonitor("Falha de conexão com Telegram; confirmação de entrega indisponível") from None
        if resposta.status_code != 200:
            raise ErroMonitor(f"Telegram HTTP {resposta.status_code}; confira credenciais, chat e limites")
        try:
            dados = resposta.json()
        except ValueError:
            raise ErroMonitor("Telegram retornou resposta inválida") from None
        if dados.get("ok") is not True:
            raise ErroMonitor("Telegram não confirmou envio")

    def fechar(self):
        self.sessao.close()


def monitorar(args, telegram):
    historico = carregar_historico(args.historico)
    pausas = {}
    ultimo_request = None
    with curl_requests.Session() as sessao:
        while True:
            produtos = carregar_produtos(args.produtos)
            for produto in produtos:
                loja = produto["loja"]
                if time.monotonic() < pausas.get(loja, 0):
                    LOG.info("%s em pausa por bloqueio", loja)
                    continue
                if ultimo_request is not None:
                    time.sleep(max(0, random.uniform(5, 15) - (time.monotonic() - ultimo_request)))
                try:
                    preco = consultar_preco(sessao, produto)
                    LOG.info("%s: %s", produto["nome"], brl(preco))
                    if deve_alertar(produto, preco, historico):
                        texto = mensagem_oferta(produto, preco)
                        if args.dry_run:
                            LOG.info("SIMULAÇÃO — alerta elegível:\n%s", texto)
                        else:
                            telegram.enviar(texto)
                            historico[produto["url"]] = {"preco": str(preco), "data": datetime.now(timezone.utc).isoformat()}
                            # Falha na persistência encerra: continuar repetiria alertas.
                            salvar_historico(args.historico, historico)
                            LOG.info("Alerta enviado e registrado")
                except Bloqueio as exc:
                    pausas[loja] = time.monotonic() + exc.espera
                    LOG.warning("%s: %s (%.0fs)", produto["nome"], exc, exc.espera)
                except curl_requests.RequestsError as exc:
                    # curl_cffi herda OSError: trate rede antes de falhas de disco.
                    LOG.error("%s: falha de rede/TLS/proxy (curl %s); seguindo para o próximo", produto["nome"], exc.code)
                except OSError:
                    raise
                except Exception as exc:
                    # Não imprime exceções de bibliotecas de rede (podem conter URLs secretas).
                    detalhe = str(exc) if isinstance(exc, ErroMonitor) else type(exc).__name__
                    LOG.error("%s: %s; seguindo para o próximo", produto["nome"], detalhe)
                finally:
                    ultimo_request = time.monotonic()
            if args.once:
                return
            LOG.info("Ciclo concluído; aguardando %.0fs", args.intervalo)
            time.sleep(args.intervalo)


def main():
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--produtos", type=Path, default=base / "produtos.json")
    parser.add_argument("--historico", type=Path, default=base / "historico.json")
    parser.add_argument("--intervalo", type=float, default=300, help="Segundos entre ciclos (mínimo 60; padrão 300)")
    parser.add_argument("--once", action="store_true", help="Executa somente um ciclo")
    parser.add_argument("--dry-run", action="store_true", help="Não envia Telegram nem modifica histórico")
    parser.add_argument("--test-telegram", action="store_true", help="Envia mensagem de teste e encerra")
    args = parser.parse_args()
    if not math.isfinite(args.intervalo) or args.intervalo < 60:
        parser.error("--intervalo deve ser finito e >= 60")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), RotatingFileHandler(base / "bot_arbitragem.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ])
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if (not args.dry_run or args.test_telegram) and (not token or not chat):
        LOG.error("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no ambiente; veja README")
        return 1
    telegram = Telegram(token, chat) if token and chat else None
    try:
        if args.test_telegram:
            telegram.enviar("✅ *Bot de arbitragem conectado* — teste de configuração\\.")
            LOG.info("Mensagem de teste confirmada pelo Telegram")
        else:
            with instancia_unica(args.historico.with_name(args.historico.name + ".lock")):
                monitorar(args, telegram)
    except KeyboardInterrupt:
        LOG.info("Monitor encerrado pelo usuário")
    except (OSError, ValueError, KeyError, ErroMonitor) as exc:
        LOG.error("Não foi possível continuar: %s", exc)
        return 1
    finally:
        if telegram:
            telegram.fechar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
