# Agente de marcação VivaGym

Automatiza a reserva da aula **V-Power**, Domingos às **10h30**, no **Ginásio de Benfica** — a tua adesão é **PRIME**. A documentação oficial da VivaGym fala em marcação "7 dias antes", mas na prática (confirmado pelo utilizador) a janela abre **6 dias antes**, ou seja, todas as **Segundas-feiras às 10h30** abre a reserva da aula de Domingo seguinte.

Duas camadas de tentativa, todas as Segundas-feiras:
1. **Rajada às 10h30** — tenta a alta frequência durante ~90s, para ganhar a corrida assim que abre.
2. **Rede de segurança, de hora a hora (10h-23h)** — se a rajada falhar (abertura atrasada, ou surgir uma vaga por cancelamento), continua a verificar sem repetir a rajada nem enviar email a cada hora; só notifica quando conseguir reservar.

Este intervalo (`BOOKING_WINDOW_DAYS`, por omissão 6) é configurável via `.env` caso a VivaGym volte a mudar a regra.

## Duas versões neste repositório

- **`booker_api.py`** (recomendado) — fala diretamente com a API interna da VivaGym (`middleware.vivagym.com`), descoberta a partir dos ficheiros JavaScript públicos da Área de Cliente. Não precisa de browser, é muito mais rápido (importante numa aula que esgota em segundos) e é o que o GitHub Actions corre.
- **`booker.py`** — versão original com Playwright (browser automatizado), mantida como reserva caso a API da VivaGym mude e a `booker_api.py` deixe de funcionar. Mais lenta, mas mais resiliente a mudanças internas (usa o mesmo site que um utilizador humano vê).

As instruções abaixo são para `booker_api.py`. Se um dia precisares da versão browser, os comandos são iguais mas trocando o nome do ficheiro, e precisas também de `python -m playwright install chromium`.

## 1. Instalar dependências

```bash
python -m pip install -r requirements.txt
```

## 2. Configurar credenciais

Copia `.env.example` para `.env` (na mesma pasta) e preenche o teu email e password reais. **Nunca partilhes este ficheiro nem coles o conteúdo no chat.**

```bash
copy .env.example .env
```

Depois edita o `.env` num editor de texto.

## 3. Mapear a Área de Cliente (importante, fazer uma vez)

Antes de confiar no agente para a marcação real, corre em modo "discover" para veres o que a API devolve com a tua conta:

```bash
python booker_api.py --discover
```

Isto faz login (localmente, com as tuas credenciais — eu nunca as vejo), lista as aulas do dia alvo no ginásio configurado, e guarda a resposta completa em `logs/discover_api_*.json`. Confirma nos logs se a aula "V-Power" aparece com a hora certa e um `bookingId` válido.

## 4. Testar sem reservar de verdade

```bash
python booker_api.py --dry-run
```

Faz tudo (login, listagem, encontrar a aula com vaga) mas não chama o endpoint de reserva. Como agora mesmo a aula está esgotada, o esperado é ficar a repetir durante a janela de tentativas e no fim reportar "FALHOU" — isso confirma que a deteção está a funcionar.

## 5. Testar localmente "a sério"

```bash
python booker_api.py
```

Espera pela hora exata de abertura (hora de Lisboa, sempre correta mesmo com mudança de hora de Verão/Inverno) e tenta reservar em loop. No final, tenta notificar o resultado (sucesso/falha/erro) por email via Resend, e por notificação do Windows se estiveres a correr localmente.

Isto é só para validares antes de avançar para a versão autónoma na cloud (secção 7). Corrida localmente, continua dependente do teu PC estar ligado.

## 6. Criar a conta Resend (email de notificação)

1. Cria uma conta grátis em [resend.com](https://resend.com) (tens de a criar tu — não posso criar contas em teu nome).
2. Gera uma **API key** (Dashboard → API Keys → Create API Key).
3. No plano grátis, sem verificares um domínio próprio, só podes enviar a partir de `onboarding@resend.dev` e apenas para o email com que te registaste na Resend — usa por isso o mesmo `hugoca@gmail.com` no registo e como `NOTIFY_EMAIL_TO`.
4. Guarda a API key — vais precisar dela no passo seguinte (nunca a coles no chat).

## 7. Publicar no GitHub e ativar a execução autónoma (GitHub Actions)

Isto faz o agente correr todas as semanas sem depender do teu PC estar ligado.

1. Cria uma conta GitHub (se ainda não tiveres) e um **repositório novo, privado** — ex. `vivagym-booker`. (Crias tu; eu não posso criar contas por ti.)
2. No teu terminal, dentro desta pasta, publica o código:
   ```bash
   git init
   git add booker.py requirements.txt .env.example .gitignore README.md .github
   git commit -m "Agente de marcacao VivaGym"
   git branch -M main
   git remote add origin https://github.com/<o-teu-utilizador>/vivagym-booker.git
   git push -u origin main
   ```
   (Confirma que **não** estás a adicionar o `.env` — o `.gitignore` já o exclui.)
3. No GitHub, vai a **Settings → Secrets and variables → Actions** do repositório:
   - Em **Secrets**, cria:
     - `VIVAGYM_EMAIL`
     - `VIVAGYM_PASSWORD`
     - `RESEND_API_KEY`
   - Em **Variables**, cria:
     - `GYM_NAME` = `Benfica`
     - `CLASS_NAME` = `V-Power`
     - `CLASS_DAY` = `Domingo`
     - `CLASS_TIME` = `10:30`
     - `NOTIFY_EMAIL_TO` = `hugoca@gmail.com`
4. O workflow já está em [.github/workflows/vivagym-booking.yml](.github/workflows/vivagym-booking.yml), agendado para todas as Segundas-feiras (dia em que a reserva abre). Para testar sem esperar pelo cron, vai a **Actions → VivaGym Booking Agent → Run workflow** (botão "workflow_dispatch") e corre manualmente uma vez.
5. Confirma no separador **Actions** que a execução terminou com sucesso, e verifica se recebeste o email de resultado.

A partir daqui, corre sozinho todas as semanas, com o teu PC ligado ou não — só precisas de verificar o email de resultado.

## Notas

- Os logs de cada execução ficam em `logs/booker_api_*.log` localmente, ou como artefacto descarregável em cada execução no GitHub Actions (separador Actions → execução → Artifacts).
- Se a VivaGym mudar a API interna (endpoints, nomes de campos), a `booker_api.py` pode falhar — corre `--discover` de novo (localmente) e partilha o `discover_api_*.json` para eu ajustar. Como reserva, há sempre a `booker.py` (versão browser) por trás.
- O ficheiro `.env` está no `.gitignore` — nunca vai para controlo de versões; na cloud, os mesmos valores vivem nos Secrets/Variables do GitHub, geridos só por ti.
- O agendamento do GitHub Actions (`cron: "25 9 * * 1"`, Segunda-feira) tem folga suficiente para cobrir hora de Verão e de Inverno em Lisboa — o script espera internamente pela hora exata, por isso não precisas de ajustar o cron duas vezes por ano.
- O ID do Ginásio de Benfica (718) é resolvido automaticamente a partir do nome via um endpoint público (`/api/v1/gyms`), não está fixo no código — se um dia mudares de ginásio, basta alterar `GYM_NAME`.
