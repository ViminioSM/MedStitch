<!-- markdownlint-disable MD033 MD041 -->
<div align="center">
  <a href="https://github.com/ViminioSM/MedStitch">
    <img alt="MedStitch Logo" width="180" src="https://github.com/ViminioSM/MedStitch/raw/dev/assets/SmartStitchLogo.png">
  </a>

  <h1>MedStitch</h1>
  <p><strong>Stitch + Slice para webtoon/manhwa/manhua</strong><br/>RÃ¡pido, estÃ¡vel e pronto para fluxo de ediÃ§Ã£o.</p>

  <p>
    <a href="https://github.com/ViminioSM/MedStitch/releases/latest"><img src="https://img.shields.io/github/v/release/ViminioSM/MedStitch?label=release" alt="Latest Release"></a>
    <a href="https://github.com/ViminioSM/MedStitch/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ViminioSM/MedStitch/ci.yml?label=ci" alt="CI"></a>
    <a href="https://github.com/ViminioSM/MedStitch/actions/workflows/build.yml"><img src="https://img.shields.io/github/actions/workflow/status/ViminioSM/MedStitch/build.yml?label=release" alt="Release Workflow"></a>
    <a href="https://github.com/ViminioSM/MedStitch/releases"><img src="https://img.shields.io/github/downloads/ViminioSM/MedStitch/total" alt="Downloads"></a>
    <a href="https://github.com/ViminioSM/MedStitch/blob/dev/LICENSE"><img src="https://img.shields.io/github/license/ViminioSM/MedStitch" alt="License"></a>
  </p>
</div>

---

## O que o MedStitch faz

MedStitch junta mÃºltiplas imagens em pÃ¡ginas longas e depois corta em painÃ©is de leitura.

Objetivos do projeto:

- Preservar qualidade visual.
- Evitar cortes ruins em texto e arte.
- Manter fluxo simples para produÃ§Ã£o.
- Atender GUI e CLI.

## Destaques

### Interface GUI

- Stitch/slice por pasta.
- Detectores:
  - Pixel comparison (smart).
  - Direct slicing.
- Formatos de saÃ­da: .png, .avif (lossless), .jpg, .webp, .bmp, .psd, .tiff, .tga.
- Enforce de largura: none, automÃ¡tico, customizado.
- Perfis e persistÃªncia de configuraÃ§Ãµes.
- PÃ³s-processamento com placeholders [stitched] e [processed].
- IntegraÃ§Ã£o opcional com ComicZip.
- Menu de contexto no Windows.
- Checagem de update e auto-update para app compilado.

### Sistema de Watermark

- Fullpage watermark em blocos uniformes.
- Overlay watermark com posiÃ§Ã£o/opacidade/escala.
- InserÃ§Ã£o de header e footer.
- Toggle rÃ¡pido via menu de contexto.

### Console (CLI)

- Pipeline para batch/headless.
- OpÃ§Ãµes principais de detector/corte via argumentos.

---

## ComeÃ§ando rÃ¡pido

### Windows (release)

1. Baixe a versÃ£o mais recente em Releases.
2. Extraia o pacote.
3. Execute SmartStitch.exe.
4. Escolha a pasta de entrada.
5. Ajuste detector/saÃ­da.
6. Inicie o processamento.

### Rodando via cÃ³digo-fonte

1. Instale Python 3.11+.
2. Instale dependÃªncias.

```bash
pip install -r requirements.txt
```

3. Rode GUI.

```bash
python SmartStitchGUI.py
```

4. Ou rode CLI.

```bash
python SmartStitchConsole.py -i "./chapter" -sh 7500 -t .png
```

---

## CLI (resumo)

```text
python SmartStitchConsole.py [-h] -i INPUT_FOLDER -sh SPLIT_HEIGHT
                             [-t {.png,.avif,.jpg,.webp,.bmp,.psd,.tiff,.tga}]
                             [-cw CUSTOM_WIDTH]
                             [-dt {none,pixel}]
                             [-s [0-100]]
                             [-lq [1-100]]
                             [-ip IGNORABLE_PIXELS]
                             [-sl [1-100]]
```

Observacao:

- Em .avif, o encode e feito em modo lossless por padrao.

---

## Build local

```bash
python -m scripts.build
```

SaÃ­da esperada:

- dist/SmartStitch/SmartStitch.exe

---

## AtualizaÃ§Ã£o automÃ¡tica no app

Endpoint usado:

- <https://api.github.com/repos/ViminioSM/MedStitch/releases/latest>

Comportamento:

- Compara versÃ£o local com tag da release.
- Se houver versÃ£o mais nova, o app compilado pode baixar ZIP, aplicar update e reiniciar.

Requisitos:

- Tags no formato vX.Y.Z.
- Release com asset .zip.

---

## Pipeline GitHub Actions

Workflows:

- .github/workflows/ci.yml
- .github/workflows/auto-tag.yml
- .github/workflows/build.yml

Fluxo:

1. Push em dev/main dispara CI (build de validaÃ§Ã£o).
2. Se o tÃ­tulo do commit tiver versÃ£o semÃ¢ntica, auto-tag cria vX.Y.Z.
3. Auto-tag dispara workflow de release.
4. Release workflow compila + publica a release com ZIP.

Nome do asset:

- MedStitch-vX.Y.Z-windows.zip

Exemplo de deploy:

```bash
git commit -m "3.2.0"
git push origin main
```

---

## Estrutura do projeto

- gui/: interface, controller e orquestraÃ§Ã£o.
- console/: launcher e fluxo CLI.
- core/detectors/: detectores de corte.
- core/services/: image IO, manipulaÃ§Ã£o, watermark, postprocess, settings.
- core/models/: modelos de configuraÃ§Ã£o e work directory.
- scripts/: build e utilitÃ¡rios.

---

## Troubleshooting

- Menu de contexto duplicado:
  - Remova pelo app.
  - Instale novamente.
- Update nÃ£o encontrado:
  - Verifique internet/acesso ao GitHub.
  - Confirme tag vÃ¡lida e ZIP na release.
- PÃ³s-processamento falhando:
  - Verifique caminho do executÃ¡vel e argumentos.

---

## Reportando problemas

Ao abrir issue, inclua:

- Passos executados.
- Comportamento esperado vs atual.
- Logs da pasta **logs**.
- Comando ou configuraÃ§Ã£o usada.

---

## LicenÃ§a

Projeto distribuÃ­do sob os termos do arquivo LICENSE.
