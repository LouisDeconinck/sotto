[English](README.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (BR)](README.pt-BR.md)

*Tradução para o português (Brasil) do [README.md](README.md). O texto original em inglês é a versão oficial e prevalece em caso de divergência.*

# Sotto

Sincronização de segredos com criptografia de ponta a ponta para equipes de desenvolvimento. Pare de enviar seu `.env` pelo Slack.

> [!WARNING]
> O Sotto está antes da versão 1.0 e **não** passou por uma auditoria criptográfica independente. Funciona de ponta a ponta, mas
> ainda não deve guardar segredos críticos de produção. Consulte [SECURITY.md](SECURITY.md).

O Sotto se baseia em uma única implementação criptográfica em Rust compartilhada pela CLI nativa e pelo cliente
do navegador via WebAssembly. O servidor armazena e sincroniza dados criptografados sem nunca receber
segredos em texto puro nem chaves utilizáveis.

## Status atual

O fluxo de ponta a ponta funciona: criptografe localmente, sincronize texto cifrado, descriptografe em outro dispositivo ou no
navegador e compartilhe um único segredo com um link de uso único. Equipes também funcionam de ponta a ponta: organizações
com papéis, concessões de ambiente por membro, rotação de chaves ao remover um membro, tokens de máquina para CI
e recuperação de conta por perda de chaves.

| Componente | Disponível agora |
| --- | --- |
| Núcleo criptográfico | KDF, AEAD XChaCha20-Poly1305 + AAD, encapsulamento de chaves, concessões seladas X25519, a hierarquia de cofres de ambiente, reencapsulamento de chaves de dados (rotação), criptografia de links de compartilhamento e codificação de chaves, com vetores de referência compartilhados entre builds nativas e WASM |
| CLI | `init`, gerenciamento local de segredos, injeção com `run`, sincronização `login`/`push`/`pull`, `setup` para novos dispositivos, `share`; equipes: `org create/ls/invite/members/remove`, `grant`, `clone`, `rotate`, `token create/ls/revoke` de máquina (com modo `SOTTO_TOKEN` para CI), `reset` com kit de emergência |
| Servidor | Login OAuth + sessões, sincronização de conta + snapshots (escritas versionadas, ETag), organizações + membros + papéis, concessões de chaves de cofre por membro, rotação transacional de chaves, tokens de máquina, redefinição de conta e links de compartilhamento, só texto cifrado |
| Web | Login (sessão com cookie), desbloqueio no navegador + descriptografia do cofre com sua própria concessão, criação e recebimento de compartilhamentos de uso único, e um painel de equipe: organizações, membros, convite por e-mail, compartilhamento de um ambiente com um membro |

## Instalação

Binários pré-compilados e assinados para macOS (Apple Silicon + Intel), Linux (x86_64 + ARM64) e
Windows x86_64:

```sh
curl -fsSL https://raw.githubusercontent.com/getsotto/sotto/main/install.sh | sh
```

```powershell
irm https://raw.githubusercontent.com/getsotto/sotto/main/install.ps1 | iex
```

O instalador verifica a soma de verificação SHA-256 do pacote, e sua assinatura Sigstore quando `cosign`
está instalado, antes de instalar (`~/.local/bin` no macOS/Linux, `%LOCALAPPDATA%\sotto\bin` no
Windows). Prefere conferir antes? Baixe um pacote na
[página de releases](https://github.com/getsotto/sotto/releases) e verifique manualmente conforme
[SECURITY.md](SECURITY.md), ou compile do código-fonte (consulte [Desenvolvimento](#desenvolvimento)).

### GitHub Actions

Para GitHub Actions, use a [action Sotto Setup](https://github.com/getsotto/sotto-action) para
instalar uma versão exata da CLI e verificar sua soma de verificação e pacotes Sigstore antes de deixar o `sotto`
disponível para as etapas seguintes:

```yaml
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
      - uses: getsotto/sotto-action@543d1af56ac81d1f1511d88c3d269106e8513a28 # merged v1.1 implementation
        with:
          sotto-version: v0.4.0
      - run: sotto run -- npm test
        env:
          SOTTO_SERVER: ${{ vars.SOTTO_SERVER }}
          SOTTO_TOKEN: ${{ secrets.SOTTO_TOKEN }}
```

A referência da action e `sotto-version` são independentes. O exemplo fixa a implementação v1.1 incorporada
pelo SHA completo do commit porque ainda não foi publicada uma release numerada da action. Mantenha
`sotto-version` como uma versão exata `vX.Y.Z`. Defina a variável de repositório opcional `SOTTO_SERVER`
para um servidor auto-hospedado. Consulte a [documentação da action](https://github.com/getsotto/sotto-action#readme)
para exemplos de matriz, Windows e workflows reutilizáveis.

## Início rápido

Quer uma demo funcional? O [**sotto-example**](https://github.com/getsotto/sotto-example)
mostra a injeção local de segredos com um GIF de Python e passos copiáveis, além de pequenos exemplos
em JavaScript, TypeScript, Java, C#, PHP, Go e C++. Não precisa de conta; as instruções cobrem
macOS, Linux e Windows.

```sh
sotto init                   # create your identity + first project - SAVE the printed Emergency Kit
sotto set DATABASE_URL       # hidden prompt; encrypted locally before it ever touches disk
sotto import .env            # optional: pull in an existing file, still encrypted locally
sotto run -- npm start       # inject the environment's secrets into any command
sotto login && sotto push    # optional: sync ciphertext via the hosted instance (getsotto.co.uk)
sotto share DATABASE_URL     # one-time, burn-after-reading link for a single secret
```

`sotto login` usa a instância hospedada em [getsotto.co.uk](https://getsotto.co.uk), a menos que você aponte
para outro lugar com `--server <url>` (consulte [Deploy](deploy/README.md) para hospedar o seu). De todo modo
o servidor só armazena texto cifrado: o cofre web no mesmo endereço descriptografa no seu
navegador, com chaves que nunca saem dos seus dispositivos.

Trabalhando em equipe:

```sh
sotto org create acme                      # prints the org id
sotto init --org <org-id>                  # an org-owned project
sotto org invite <org-id> dev@example.com  # invite an existing Sotto user
sotto grant <user-id>                      # share the active environment (they run `sotto clone`)
sotto token create --name ci               # SOTTO_TOKEN: run/export in CI, no password needed
```

## Arquitetura

```text
CLI (native) ─────┐
                  ├── sotto-core ── versioned encrypted data
Web client (WASM) ┘                         │
                                            ▼
                                  sync/API server
                                  (ciphertext only)
```

O workspace contém quatro crates:

- `crates/core`: os tipos criptográficos compartilhados e a implementação criptográfica completa.
- `crates/cli`: a interface de linha de comando `sotto` e o principal cliente nativo.
- `crates/server`: a API de sincronização baseada em Axum.
- `crates/wasm`: bindings `wasm-bindgen` que expõem o núcleo aos clientes web.

## Pré-requisitos

- [Rustup](https://rustup.rs/) com Rust estável 1.89 ou mais recente
- Os componentes `clippy` e `rustfmt`
- O target `wasm32-unknown-unknown`

O `rust-toolchain.toml` incluído pede ao Rustup para instalar automaticamente os componentes e o target necessários.

## Desenvolvimento

Clone o repositório e compile e teste o workspace completo:

```sh
git clone https://github.com/getsotto/sotto.git
cd sotto

cargo build --workspace
cargo test --workspace
```

Use a CLI localmente (sem servidor):

```sh
cargo run -p sotto-cli -- --help
cargo run -p sotto-cli -- init                 # create an identity + project; prints your Emergency Kit
cargo run -p sotto-cli -- set DATABASE_URL     # hidden prompt
cargo run -p sotto-cli -- run -- your-command  # inject secrets as env vars into a subprocess
```

Os segredos ficam criptografados em repouso em um banco SQLite local; a chave mestra fica no chaveiro do SO
com um TTL. Sincronizar com um servidor (`login`/`push`/`pull`/`setup`/`share`) é opcional.

### Executando o servidor

O servidor precisa de Postgres (um `docker compose up -d` sobe um para uso local):

```sh
DATABASE_URL=postgres://sotto:sotto@localhost:5432/sotto cargo run -p sotto-server
curl http://127.0.0.1:8080/health   # → ok
```

O login com GitHub OAuth requer `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`. Para qualquer deploy não local,
defina também `SOTTO_PUBLIC_URL` como a origem pública do servidor: ela monta a URL de callback do GitHub
e precisa ser igual ao callback registrado no app OAuth (o padrão é `http://localhost:8080`); e, para o cliente
web, `SOTTO_WEB_ORIGIN`. Sem OAuth o servidor continua subindo (serve `/health` e roda as migrations), mas o login
e todos os endpoints autenticados (sync, criação de compartilhamentos) ficam indisponíveis.

### Cliente web

O cliente do navegador roda o mesmo núcleo criptográfico via WebAssembly (`web/`):

```sh
cd web
npm ci
npm run dev      # dev server (proxies the API to localhost:8080)
npm run build    # production bundle → web/dist (strict CSP + Subresource Integrity)
```

### Deploy

Um comando sobe uma instância hospedada completa: Postgres, servidor e Caddy com HTTPS automático, a partir de
[`deploy/docker-compose.prod.yml`](deploy/docker-compose.prod.yml); o passo a passo está em
[`deploy/README.md`](deploy/README.md). As partes também funcionam separadas: sirva o app web e a API na
**mesma origem** (para que o cookie de sessão e a CSP permaneçam na mesma origem); o [`Caddyfile`](Caddyfile)
incluído serve `web/dist` e faz proxy reverso da API, com cabeçalhos de segurança; o [`Dockerfile`](Dockerfile)
monta a imagem do servidor (as migrations rodam na inicialização).

## Verificações de desenvolvimento

Rode as mesmas verificações básicas usadas pelo CI:

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

A política de cadeia de suprimentos está definida em `deny.toml` e é verificada no CI com
[`cargo-deny`](https://github.com/EmbarkStudios/cargo-deny):

```sh
cargo deny check
```

A auditoria completa do lockfile também roda no job `supply-chain` do CI (requer Python 3.11+):

```sh
cargo install cargo-audit --version 0.22.2 --locked
python3 -B -m unittest discover -s scripts/tests -v
scripts/check-cargo-audit
```

A [política de auditoria](.ci/cargo-audit-policy.toml) registra exceções exatas para as entradas adormecidas `rsa` e
`spin` do lockfile. Cada uma precisa bater em pacote, versão, registro e achado, e não ter nenhum caminho de dependência
normal, de build ou de teste em nenhum target, com as features padrão e todas as do workspace. Novos achados, identidades
alteradas, pacotes alcançáveis, varreduras com falha e exceções obsoletas quebram o CI. Remova uma exceção no mesmo PR que
remover seu achado; uma lista de exceções vazia exige uma auditoria limpa. O `cargo audit` puro continua relatando esses
achados: esta política documenta e verifica sua dormência, e não afirma que as releases vulneráveis ou retiradas foram
corrigidas. Os [padrões de auditoria](.cargo/audit.toml) do projeto mantêm a busca de advisories e as verificações de
retiradas ativadas e sobrescrevem a configuração global de auditoria do desenvolvedor. Não adicione ignorados de advisories
nem filtros de varredura. O verificador também roda uma auditoria em formato de terminal para detectar falhas de registro que
o cargo-audit 0.22.2 pode omitir na saída JSON, para que uma varredura indisponível de pacotes retirados não conte como
resultado limpo.

O portão entre implementações prova que os builds nativo e WASM concordam: texto cifrado produzido no nativo
descriptografa byte a byte no WASM a partir de vetores de referência compartilhados:

```sh
wasm-pack test --node crates/wasm
```

O build web e sua auditoria de dependências rodam no CI (`.github/workflows/ci.yml`).

## Telemetria

O **servidor** envia um ping anônimo por dia (nos primeiros 10-20 minutos após o boot) para
`https://getsotto.co.uk/telemetry/v1/ping`, para contar instâncias ativas e ver quais
versões estão por aí. A resposta nomeia a release mais recente, e o servidor registra uma linha quando está rodando
uma versão desatualizada. Este é o payload **inteiro**: o código de envio está em
[`crates/server/src/telemetry.rs`](crates/server/src/telemetry.rs), e um teste unitário fixa o payload em exatamente
estes quatro campos:

```json
{ "instance_id": "0d0972a6-…", "version": "0.2.0", "os": "linux", "arch": "x86_64" }
```

`instance_id` é um UUID aleatório gerado uma vez e guardado no seu banco de dados, derivado de nada, então não identifica
hardware, host ou conta; apagá-lo transforma a instância em um contador anônimo novo. O lado da ingestão não guarda
endereços IP nem localização derivada. Sem contagens de orgs, membros ou segredos, e sem eventos de uso.
A **CLI, o cliente web e o WASM nunca enviam nada**.

Opte por sair com `SOTTO_TELEMETRY=off` (ou o [`DO_NOT_TRACK=1`](https://consoledonottrack.com) válido para todas as
ferramentas): desativada, a tarefa nunca é iniciada e nenhuma requisição é feita. `SOTTO_TELEMETRY_URL` redireciona o ping
(por exemplo, para agregar uma frota privada), e registros inativos há 12 meses são expurgados do censo hospedado.

## Segurança

O modelo do Sotto é de conhecimento zero: segredos em texto puro e chaves de descriptografia utilizáveis ficam nos
dispositivos cliente, e o servidor vê só texto cifrado mais metadados mínimos. Isto está implementado, mas **ainda não
auditado de forma independente**: consulte [SECURITY.md](SECURITY.md) para o modelo, a exposição honesta de metadados,
como a superfície web (recarregada, mais fraca) é reforçada e como verificar releases assinadas. O modelo completo de
adversários, garantias e não objetivos explícitos está publicado em [THREAT-MODEL.md](THREAT-MODEL.md). Relate
vulnerabilidades em privado conforme SECURITY.md.

## Contribuindo

O Sotto é Apache-2.0 e aceita contribuições. Comece por [CONTRIBUTING.md](CONTRIBUTING.md);
[good first issues](https://github.com/getsotto/sotto/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
estão marcadas, e perguntas que não são bugs vão para
[Discussions](https://github.com/getsotto/sotto/discussions).

Relate vulnerabilidades em privado conforme [SECURITY.md](SECURITY.md).

## Licença

Licenciado sob a [Licença Apache, versão 2.0](LICENSE): todos os crates e o cliente web. Você só pode usar este projeto
em conformidade com a Licença. Exceto quando exigido pela lei aplicável ou acordado por escrito, o software distribuído
sob a Licença é distribuído "COMO ESTÁ", SEM GARANTIAS OU CONDIÇÕES DE QUALQUER TIPO.
