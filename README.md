# Bot gratuito de ofertas para revenda

Monitor em Python 3.10+ para Windows e Linux (incluindo este ambiente na nuvem e WSL). Usa `curl_cffi` com impersonação TLS de Chrome e User-Agent correspondente, BeautifulSoup4 para HTML e `requests` para Telegram. Não usa APIs pagas. O computador precisa ficar ligado; não inclui hospedagem gratuita permanente.

## Windows (Prompt de Comando / CMD)

Abra o CMD na pasta extraída do projeto e execute:

```bat
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python bot_arbitragem.py --once --dry-run
```

O bloqueio do histórico usa `msvcrt`, que já vem com o Python do Windows. **Não instale `fcntl` pelo pip**: esse módulo é utilizado apenas no Unix.

Para definir as credenciais com entrada mascarada do token, abra o **PowerShell** na mesma pasta:

```powershell
$env:TELEGRAM_BOT_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Token do bot' -AsSecureString)).Password
$env:TELEGRAM_CHAT_ID = Read-Host 'CHAT_ID'
.\.venv\Scripts\python.exe bot_arbitragem.py --test-telegram
.\.venv\Scripts\python.exe bot_arbitragem.py
```

As variáveis valem somente nessa sessão do PowerShell. Se já as configurou em outro terminal, execute por ele. O tutorial de `getUpdates` abaixo usa Bash; no Windows, use esse código Python em um arquivo local `obter_chat_id.py` e rode `.\.venv\Scripts\python.exe obter_chat_id.py` na sessão com o token configurado.

Se uma versão anterior exibir **`Não foi possível continuar: not readable`**, pare o bot e atualize `bot_arbitragem.py` com a versão atual do GitHub. Preserve seu `produtos.json` e `historico.json`. O arquivo de lock agora é aberto para leitura e escrita, com backend próprio para cada sistema. Erros de permissão diferentes disso devem ser verificados pelo caminho/arquivo indicado.

## Instalação e execução

Na pasta `/workspace/revenda-bot` (ou na pasta onde clonou o projeto):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install curl_cffi==0.13.0 beautifulsoup4==4.14.2 requests==2.32.5
# Equivalente: python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python bot_arbitragem.py --once --dry-run
```

O modo de simulação consulta as lojas, mostra alertas elegíveis no terminal e não envia mensagens nem altera `historico.json`. A suíte de testes usa HTML local e simulações de rede; não envia mensagens.

Configure `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` nas variáveis seguras do seu ambiente. Em um terminal Bash, pode definir para a sessão sem registrar o token no histórico de comandos:

```bash
read -rsp 'Token do bot: ' TELEGRAM_BOT_TOKEN; echo
export TELEGRAM_BOT_TOKEN
read -rp 'CHAT_ID: ' TELEGRAM_CHAT_ID
export TELEGRAM_CHAT_ID
python bot_arbitragem.py --test-telegram
python bot_arbitragem.py
```

O programa não carrega `.env` automaticamente. Nunca comite credenciais. Para parar, pressione `Ctrl+C`. Uma execução contínua faz novo ciclo a cada 300 segundos **após** terminar a lista, com pausa aleatória de 5–15 segundos entre requisições às lojas. Ajuste com `--intervalo 600` (mínimo 60). Para uma única rodada com envio real, use `--once`.

## Telegram em cerca de 3 minutos

1. No Telegram, abra o **@BotFather** oficial: <https://t.me/BotFather>. Envie `/newbot`, escolha um nome e um username disponível terminado em `bot`. Ele devolverá um **token**. Guarde-o nas variáveis seguras como `TELEGRAM_BOT_TOKEN`; não o envie em chats nem o publique.
2. Abra o link do seu novo bot e envie `/start`. Isso permite ao bot conversar com você. O exemplo usa conversa privada; para grupos, o ID normalmente é negativo e o bot precisa participar do grupo.
3. Depois de definir o token no terminal como acima, rode este comando. Ele chama a API **gratuita** do Telegram e imprime apenas IDs, sem imprimir o token ou o conteúdo das mensagens:

   ```bash
   python - <<'PY'
   import os
   import requests
   try:
       r = requests.get(
           'https://api.telegram.org/bot' + os.environ['TELEGRAM_BOT_TOKEN'] + '/getUpdates',
           timeout=20,
       )
       data = r.json()
       if not r.ok or not data.get('ok'):
           raise ValueError('Resposta não confirmada')
       ids = {u['message']['chat']['id'] for u in data.get('result', []) if 'message' in u}
       print('CHAT_ID disponíveis:', ', '.join(map(str, sorted(ids))) or 'Nenhum: envie /start ao seu bot e repita.')
   except Exception:
       print('Falha: confira token, conexão e se este bot já usa webhook/getUpdates em outro processo.')
   PY
   ```

   Salve o ID da sua conversa em `TELEGRAM_CHAT_ID`. Execute `python bot_arbitragem.py --test-telegram` para confirmar a entrega. Se não houver atualizações, envie outra mensagem ao bot e repita. Um bot novo não precisa de webhook; não use o mesmo token em outro consumidor de atualizações durante a configuração.

## Cadastro de produtos

`produtos.json` inclui cinco links de exemplo de produtos conhecidos: Echo Dot, Echo Pop, SSD Kingston A400 e mouses Logitech G203 e M90. **Não são ofertas verificadas nem preços atuais.** Confira se cada anúncio ainda existe e corresponde ao modelo/capacidade/cor desejados antes de ativar alertas. Os valores de compra e revenda são hipóteses ilustrativas, não uma pesquisa de liquidez ou garantia de lucro.

Para começar com 10–20 links, duplique objetos e substitua os campos por anúncios diretos conferidos por você. São aceitas URLs HTTPS de Amazon Brasil, Mercado Livre (incluindo `produto.mercadolivre.com.br`) e Kabum. Não use links encurtados, pesquisas, páginas de categoria ou carrinho. Exemplo de objeto:

```json
{
  "nome": "SSD Kingston A400 480 GB SATA",
  "url": "https://www.amazon.com.br/dp/B01N5IB20Q",
  "preco_alvo": 160.00,
  "preco_revenda_estimado": 240.00
}
```

Use números JSON com ponto decimal e sem `R$`. A lista é relida no início de cada ciclo; itens individuais inválidos são registrados e ignorados. Um JSON inteiro inválido ou uma lista sem produtos válidos encerra o programa com erro para você corrigir a configuração.

## Regras de alerta e memória

- `lucro = preco_revenda_estimado × 0.84 − preco_atual`.
- `ROI = lucro ÷ preco_atual × 100`.
- Alerta somente se `preco_atual <= preco_alvo` **e** o lucro estimado for positivo.
- Com compra a R$ 1.200 e revenda a R$ 1.750: lucro estimado de **R$ 270**, ROI de **22,50%**.
- O preço é gravado por URL em `historico.json` somente depois de o Telegram confirmar o envio. Um novo alerta para a mesma URL exige preço **estritamente menor** que o último alertado, mesmo após reiniciar o programa. O histórico não é zerado quando o preço sobe.

Mensagens usam MarkdownV2 com escapes e link clicável. A gravação de histórico é atômica e um lock impede duas instâncias usando o mesmo histórico. Se o arquivo estiver corrompido ou não puder ser gravado, o processo encerra para evitar spam. Não apague o histórico a cada execução. Para recomeçar deliberadamente o acompanhamento, pare o bot e remova apenas a entrada desejada, mantendo um backup.

Não há garantia de entrega exatamente uma vez: uma queda após o Telegram receber a mensagem, mas antes da confirmação/gravação local, pode provocar repetição na próxima execução.

## Bloqueios, extração e limitações

Erros de um produto são registrados no terminal e em `bot_arbitragem.log` (rotação de 2 MB, três backups); o próximo produto continua. HTTP 403/429/503 e CAPTCHA suspendem a loja por pelo menos 15 minutos, respeitando `Retry-After` maior. Não há tentativa de resolver CAPTCHA. Impersonação, rotação de identidade e intervalos reduzem requisições repetitivas, mas não garantem acesso nem impedem bloqueios.

Os seletores priorizam o preço principal à vista/Pix e evitam preços riscados e parcelas. JSON-LD é fallback somente quando há um Product inequívoco e uma oferta BRL com preço único; respostas ambíguas geram erro em vez de escolher o menor número da página. Páginas que exigem JavaScript, login ou seletores diferentes podem não funcionar com este extrator. Atualizações dos seletores ficam concentradas em `extrair_preco`.

Confirme no checkout estoque, vendedor, variação, condições de Pix, CEP, frete e aplicação de cupons. A estimativa desconta **somente** a taxa de 16% solicitada; não inclui impostos, frete, embalagem, devoluções, custo de capital ou variações reais de taxa do marketplace. Não constitui lucro líquido contábil garantido.

O acesso externo exige permitir `www.amazon.com.br`, `www.kabum.com.br`, `www.mercadolivre.com.br`, `produto.mercadolivre.com.br` (e os hosts exatos dos seus links) e `api.telegram.org` na política de rede. Os pacotes vêm de `pypi.org` e `files.pythonhosted.org`. Token e ID são necessários apenas para envio real; `--dry-run` funciona sem eles. Respeite as condições de acesso das lojas.

## Validação realizada neste ambiente

Instalação concluída e dependências compatíveis (`pip check`). Os 19 testes passaram, incluindo HTTP em servidor local com os três perfis Chrome, mensagem Telegram simulada, parsing das três lojas, cálculos e memória. A rodada real de simulação encontrou falha CONNECT 403 do proxy de saída; nenhum preço externo foi validado. O bot continuou processando os cinco itens. Entrega real ao Telegram não foi testada, pois as duas variáveis ainda não estavam configuradas.

Foi salvo um rascunho de ambiente com instalação, instruções de execução, os cinco domínios acima e requisitos para as variáveis do Telegram. Nas configurações do ambiente, revise e salve essas alterações, preencha as variáveis com segurança e publique para reutilizar o ambiente. Salvar o rascunho não aplica a política nem publica automaticamente. Depois, repita `--once --dry-run` e `--test-telegram` para validar o acesso real.
