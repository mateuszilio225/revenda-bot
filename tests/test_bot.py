import argparse
import errno
from decimal import Decimal as D
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import requests

import bot_arbitragem as bot


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.produto = {
            "nome": "Produto [teste]!", "url": "https://www.amazon.com.br/dp/EXEMPLO",
            "loja": "amazon", "preco_alvo": D("1200"), "preco_revenda_estimado": D("1750"),
        }

    def test_dinheiro(self):
        for entrada in ("R$ 1.234,56", "1234.56", "1.234,56", D("1234.56")):
            self.assertEqual(bot.dinheiro(entrada), D("1234.56"))
        self.assertEqual(bot.dinheiro("1.200"), D("1200"))
        self.assertEqual(bot.dinheiro(D("1.200")), D("1.20"))
        for entrada in (True, "NaN", "-1", "0", "12x R$ 99,99", ""):
            with self.assertRaises(ValueError):
                bot.dinheiro(entrada)

    def test_lucro_roi(self):
        self.assertEqual(bot.calcular_lucro(D("1200"), D("1750")), (D("270.00"), D("22.50")))

    def test_elegibilidade_e_historico(self):
        p = self.produto
        self.assertTrue(bot.deve_alertar(p, D("1200"), {}))
        self.assertFalse(bot.deve_alertar(p, D("1200.01"), {}))
        h = {p["url"]: {"preco": "1100.00"}}
        self.assertFalse(bot.deve_alertar(p, D("1100"), h))
        self.assertFalse(bot.deve_alertar(p, D("1150"), h))
        self.assertTrue(bot.deve_alertar(p, D("1099.99"), h))
        self.assertFalse(bot.deve_alertar({**p, "preco_revenda_estimado": D("1000")}, D("1000"), {}))

    def test_amazon_ignora_parcelas_e_riscado(self):
        html = '''<span class="a-offscreen">R$ 80,00</span>
        <div id="corePriceDisplay_desktop_feature_div">
        <span class="a-text-price"><span class="a-offscreen">R$ 1.800,00</span></span>
        <span class="priceToPay"><span class="a-offscreen">R$ 1.199,90</span></span></div>'''
        self.assertEqual(bot.extrair_preco(html, "amazon"), D("1199.90"))

    def test_mercado_livre_centavos(self):
        html = '''<div class="ui-pdp-price__second-line"><s class="andes-money-amount andes-money-amount--previous">2000</s>
        <span class="andes-money-amount"><span class="andes-money-amount__fraction">1.199</span>
        <span class="andes-money-amount__cents">90</span></span></div>'''
        self.assertEqual(bot.extrair_preco(html, "mercadolivre"), D("1199.90"))

    def test_kabum_preco_vista(self):
        self.assertEqual(bot.extrair_preco('<h4 id="valorVista">R$ 99,99</h4>', "kabum"), D("99.99"))

    def test_jsonld_product(self):
        html = '<script type="application/ld+json">' + json.dumps({"@graph": [
            {"@type": "Product", "offers": {"@type": "Offer", "price": "1199.90", "priceCurrency": "BRL", "availability": "https://schema.org/InStock"}},
        ]}) + '</script>'
        self.assertEqual(bot.extrair_preco(html, "kabum"), D("1199.90"))
        with self.assertRaises(bot.ErroMonitor):
            bot.extrair_preco(html.replace("InStock", "OutOfStock"), "kabum")

    def test_sem_seletor_falha_em_vez_de_inventar_preco(self):
        with self.assertRaises(bot.ErroMonitor):
            bot.extrair_preco('<div>10x R$ 10,00</div>', "amazon")

    def test_jsonld_nao_usa_recomendacao_nem_faixa_de_preco(self):
        for dados in (
            {"@type": "ItemList", "itemListElement": [{"@type": "Product", "offers": {"price": 1, "priceCurrency": "BRL"}}]},
            {"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": 1, "priceCurrency": "BRL"}},
            [{"@type": "Product", "offers": {"price": n, "priceCurrency": "BRL"}} for n in (1, 2)],
        ):
            with self.assertRaises(bot.ErroMonitor):
                bot.extrair_preco('<script type="application/ld+json">' + json.dumps(dados) + '</script>', "kabum")

    def test_urls(self):
        self.assertEqual(bot.loja_da_url("https://produto.mercadolivre.com.br/MLB-123"), "mercadolivre")
        for url in ("http://www.amazon.com.br/dp/X", "https://amazon.com.br.evil.test/X", "https://localhost/X", "https://user:password@amazon.com.br/X"):
            with self.assertRaises(ValueError):
                bot.loja_da_url(url)

    def test_bloqueio_e_retry_after(self):
        sessao = Mock()
        sessao.get.return_value = Mock(status_code=429, headers={"Retry-After": "1800"})
        with self.assertRaises(bot.Bloqueio) as ctx:
            bot.consultar_preco(sessao, self.produto)
        self.assertEqual(ctx.exception.espera, 1800)
        self.assertEqual(bot.espera_retry_after("inválido"), 900)

    def test_captcha(self):
        sessao = Mock()
        sessao.get.return_value = Mock(status_code=200, headers={"Content-Type": "text/html"}, text="Robot Check")
        with self.assertRaises(bot.Bloqueio):
            bot.consultar_preco(sessao, self.produto)

    def test_historico_atomico_e_lock(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "historico.json"
            dados = {self.produto["url"]: {"preco": "100.00"}}
            self.assertEqual(bot.carregar_historico(arquivo), {})
            bot.salvar_historico(arquivo, dados)
            self.assertEqual(bot.carregar_historico(arquivo), dados)
            bot.salvar_historico(arquivo, dados)
            self.assertEqual(len(list(Path(pasta).glob("*.tmp"))), 0)
            lock = Path(pasta) / "lock"
            with bot.instancia_unica(lock):
                with self.assertRaises(bot.ErroMonitor):
                    with bot.instancia_unica(lock):
                        pass
            arquivo.write_text("{corrompido", encoding="utf-8")
            with self.assertRaises(ValueError):
                bot.carregar_historico(arquivo)

    def test_mensagem_markdown(self):
        mensagem = bot.mensagem_oferta(self.produto, D("1200"))
        self.assertIn(r"Produto \[teste\]\!", mensagem)
        self.assertIn("R$ 270,00", mensagem)
        self.assertIn("22,50%", mensagem)
        self.assertIn("[Abrir produto](https://", mensagem)

    def test_lock_em_outro_processo_e_liberacao_apos_erro(self):
        with tempfile.TemporaryDirectory() as pasta:
            lock = Path(pasta) / "lock"
            codigo = (
                "from pathlib import Path; import bot_arbitragem as b; import sys\n"
                "try:\n"
                "    with b.instancia_unica(Path(sys.argv[1])): pass\n"
                "except b.ErroMonitor:\n"
                "    sys.exit(3)\n"
            )
            comando = [sys.executable, "-c", codigo, str(lock)]
            raiz = Path(bot.__file__).parent
            with bot.instancia_unica(lock):
                self.assertEqual(subprocess.run(comando, cwd=raiz, capture_output=True).returncode, 3)
            self.assertEqual(subprocess.run(comando, cwd=raiz, capture_output=True).returncode, 0)
            with self.assertRaisesRegex(RuntimeError, "interrompido"):
                with bot.instancia_unica(lock):
                    raise RuntimeError("interrompido")
            self.assertEqual(subprocess.run(comando, cwd=raiz, capture_output=True).returncode, 0)

    def test_backend_windows_bloqueia_e_libera_o_mesmo_byte(self):
        chamadas = []
        backend = Mock(LK_NBLCK=1, LK_UNLCK=0)

        def locking(fd, modo, tamanho):
            self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
            self.assertEqual(os.read(fd, 1), b"\0")
            chamadas.append((modo, tamanho))

        backend.locking.side_effect = locking
        with tempfile.TemporaryDirectory() as pasta, patch.object(bot, "WINDOWS", True), patch.object(bot, "msvcrt", backend, create=True):
            caminho = Path(pasta) / "lock"
            for _ in range(2):
                with bot.instancia_unica(caminho):
                    pass
            self.assertEqual(caminho.read_bytes(), b"\0")
        self.assertEqual(chamadas, [(1, 1), (0, 1), (1, 1), (0, 1)])

    def test_backend_windows_nao_desbloqueia_se_nao_adquiriu(self):
        backend = Mock()
        backend.locking.side_effect = OSError(errno.EACCES, "ocupado")
        with tempfile.TemporaryDirectory() as pasta, patch.object(bot, "WINDOWS", True), patch.object(bot, "msvcrt", backend, create=True):
            with self.assertRaises(bot.ErroMonitor):
                with bot.instancia_unica(Path(pasta) / "lock"):
                    self.fail("Não deve entrar sem adquirir lock")
        backend.locking.assert_called_once()

    def test_lock_aberto_para_leitura_e_escrita(self):
        if bot.WINDOWS:
            self.skipTest("flock é o backend Unix")
        original = bot.fcntl.flock

        def verificar(arquivo, flags):
            self.assertTrue(arquivo.readable())
            self.assertTrue(arquivo.writable())
            return original(arquivo, flags)

        with tempfile.TemporaryDirectory() as pasta, patch.object(bot.fcntl, "flock", side_effect=verificar):
            with bot.instancia_unica(Path(pasta) / "lock"):
                pass

    def test_telegram_sem_vazar_token(self):
        telegram = bot.Telegram("TOKEN_SECRETO", "123")
        try:
            with patch.object(telegram.sessao, "post", side_effect=requests.Timeout("https://botTOKEN_SECRETO")):
                with self.assertRaises(bot.ErroMonitor) as ctx:
                    telegram.enviar("teste")
                self.assertNotIn("TOKEN_SECRETO", str(ctx.exception))
            with patch.object(telegram.sessao, "post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as post:
                telegram.enviar("teste")
                self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "MarkdownV2")
        finally:
            telegram.fechar()

    def test_ciclo_continua_apos_falha_e_nao_grava_envio_falho(self):
        with tempfile.TemporaryDirectory() as pasta:
            args = argparse.Namespace(historico=Path(pasta) / "historico.json", produtos=Path(pasta) / "p.json", once=True, dry_run=False, intervalo=300)
            segundo = {**self.produto, "url": "https://www.amazon.com.br/dp/SEGUNDO"}
            telegram = Mock()
            with patch.object(bot, "carregar_produtos", return_value=[self.produto, segundo]), patch.object(bot, "consultar_preco", side_effect=[bot.ErroMonitor("HTML mudou"), D("1000")]), patch.object(bot.time, "sleep"):
                bot.monitorar(args, telegram)
            telegram.enviar.assert_called_once()
            self.assertIn(segundo["url"], bot.carregar_historico(args.historico))
            args.historico.unlink()
            telegram.enviar.side_effect = bot.ErroMonitor("Falha Telegram")
            with patch.object(bot, "carregar_produtos", return_value=[self.produto]), patch.object(bot, "consultar_preco", return_value=D("1000")):
                bot.monitorar(args, telegram)
            self.assertFalse(args.historico.exists())

    def test_dry_run_nao_envia_ou_grava(self):
        with tempfile.TemporaryDirectory() as pasta:
            args = argparse.Namespace(historico=Path(pasta) / "historico.json", produtos=Path(pasta) / "p.json", once=True, dry_run=True, intervalo=300)
            telegram = Mock()
            with patch.object(bot, "carregar_produtos", return_value=[self.produto]), patch.object(bot, "consultar_preco", return_value=D("1000")):
                bot.monitorar(args, telegram)
            telegram.enviar.assert_not_called()
            self.assertFalse(args.historico.exists())

    def test_falha_curl_nao_interrompe_ciclo(self):
        with tempfile.TemporaryDirectory() as pasta:
            args = argparse.Namespace(historico=Path(pasta) / "h.json", produtos=Path(pasta) / "p.json", once=True, dry_run=True, intervalo=300)
            with patch.object(bot, "carregar_produtos", return_value=[self.produto, self.produto]), patch.object(bot, "consultar_preco", side_effect=[bot.curl_requests.RequestsError("proxy indisponível", code=56), D("1000")]) as consultar, patch.object(bot.time, "sleep"):
                bot.monitorar(args, None)
            self.assertEqual(consultar.call_count, 2)

    def test_clientes_http_reais_em_servidor_local(self):
        recebidos = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                recebidos.append(self.headers.get("User-Agent"))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'<div id="corePrice_feature_div"><span class="priceToPay"><span class="a-offscreen">R$ 999,90</span></span></div>')

            def do_POST(self):
                recebidos.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true, "result": {"message_id": 1}}')

        servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=servidor.serve_forever, daemon=True)
        thread.start()
        endereco = f"http://127.0.0.1:{servidor.server_port}"
        telegram = bot.Telegram("token-local-ficticio", "123")
        telegram.endpoint = endereco + "/sendMessage"
        try:
            with bot.curl_requests.Session() as sessao:
                for navegador in bot.NAVEGADORES:
                    with patch.object(bot.random, "choice", return_value=navegador):
                        self.assertEqual(bot.consultar_preco(sessao, {**self.produto, "url": endereco}), D("999.90"))
            telegram.enviar(bot.mensagem_oferta(self.produto, D("999.90")))
            self.assertEqual(len(recebidos), 4)
            self.assertEqual(recebidos[-1]["chat_id"], "123")
        finally:
            telegram.fechar()
            servidor.shutdown()
            servidor.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
