# Agente de marcação VivaGym

Automatiza a reserva da aula **V-Power**, Domingos às **10h30**, no **Ginásio de Benfica** — a tua adesão é **PRIME**, pelo que a marcação abre exatamente **7 dias antes**, ou seja, todos os Domingos às 10h30 (quando essa aula começa) abre a reserva da aula de daqui a uma semana.

## 1. Instalar dependências

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 2. Configurar credenciais

Copia `.env.example` para `.env` (na mesma pasta) e preenche o teu email e password reais. **Nunca partilhes este ficheiro nem coles o conteúdo no chat.**

```bash
copy .env.example .env
```

Depois edita o `.env` num editor de texto.

## 3. Mapear a Área de Cliente (importante, fazer uma vez)

Antes de confiar no agente para a marcação real, corre em modo "discover" para veres a estrutura da tua Área de Cliente depois do login:

```bash
python booker.py --discover --headed
```

Isto faz login com as tuas credenciais (só no teu PC, eu nunca as vejo) e guarda em `logs/`:
- um screenshot de página inteira (`discover_*.png`)
- um JSON com todos os links e botões visíveis (`discover_*.json`)

**Podes partilhar comigo esses dois ficheiros** (não contêm a password) para eu afinar com precisão os seletores em `booker.py` (a função `goto_booking_area`, `select_gym` e `find_class_row` estão feitas com heurísticas por texto — funcionam provavelmente, mas convém confirmar com o layout real).

## 4. Testar sem reservar de verdade

```bash
python booker.py --dry-run --headed
```

Faz tudo (login, navegação, encontrar a aula) mas não clica em "Reservar". Confirma nos logs se encontrou a aula certa.

## 5. Testar localmente "a sério"

```bash
python booker.py
```

Corre em modo invisível (headless), faz login, espera pela hora exata de abertura (hora de Lisboa, sempre correta mesmo com mudança de hora de Verão/Inverno) e tenta reservar em loop durante ~90 segundos. No final, tenta notificar o resultado (sucesso/falha/erro) por email via Resend, e por notificação do Windows se estiveres a correr localmente.

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
4. O workflow já está em [.github/workflows/vivagym-booking.yml](.github/workflows/vivagym-booking.yml), agendado para todos os Domingos. Para testar sem esperar pelo cron, vai a **Actions → VivaGym Booking Agent → Run workflow** (botão "workflow_dispatch") e corre manualmente uma vez.
5. Confirma no separador **Actions** que a execução terminou com sucesso, e verifica se recebeste o email de resultado.

A partir daqui, corre sozinho todas as semanas, com o teu PC ligado ou não — só precisas de verificar o email de resultado.

## Notas

- Os logs de cada execução ficam em `logs/booker_*.log` localmente, ou como artefacto descarregável em cada execução no GitHub Actions (separador Actions → execução → Artifacts).
- Se a VivaGym mudar o site, os seletores heurísticos podem falhar — corre `--discover` de novo (localmente) e partilha os ficheiros para eu atualizar o `booker.py`, depois faz `git push` para atualizar a versão na cloud.
- O ficheiro `.env` está no `.gitignore` — nunca vai para controlo de versões; na cloud, os mesmos valores vivem nos Secrets/Variables do GitHub, geridos só por ti.
- O agendamento do GitHub Actions (`cron: "25 9 * * 0"`) tem folga suficiente para cobrir hora de Verão e de Inverno em Lisboa — o script espera internamente pela hora exata, por isso não precisas de ajustar o cron duas vezes por ano.
