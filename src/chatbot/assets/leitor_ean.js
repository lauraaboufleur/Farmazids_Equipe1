// Leitor de código de barras (EAN) do chatbot da Farmazids.
// - O botão do cabeçalho (.ean-botao, desenhado pelo app.py) abre uma janela com a câmera
//   ao vivo (a traseira no celular) e um retângulo-guia.
// - Quem lê o código é o BarcodeDetector do navegador, quando existe; senão, a biblioteca
//   ZXing (@zxing/browser, versão fixa abaixo), carregada da CDN só na primeira vez.
// - Ao ler um EAN válido (8 ou 13 dígitos, dígito verificador conferido), a câmera desliga,
//   a janela fecha e a pergunta vai pelo mesmo caminho da pesquisa por voz
//   (window.farmazidsVoz.perguntar, no voz.js).
// - Sem câmera (negada ou inexistente), a janela oferece digitar o código.
// O app.py carrega este arquivo a cada execução; ele só se instala uma vez por página.
// O sinal de menor seguido de letra ou barra não pode aparecer aqui (ver o voz.js).
(function () {
  if (window.farmazidsEan) return;
  const ZXING = "https://cdn.jsdelivr.net/npm/@zxing/browser@0.2.1/umd/zxing-browser.min.js";
  const PERGUNTA = "Compare os preços do produto com código de barras ";
  const INTERVALO = 120; // ms entre duas tentativas de leitura
  const estado = {
    aberto: false,
    stream: null,      // câmera ligada
    timer: null,       // próxima tentativa de leitura
    janela: null,
    zxing: null,       // promessa do carregamento da biblioteca
  };
  window.farmazidsEan = estado;

  // ---------- EAN ----------
  // Dígito verificador: da direita para a esquerda (sem ele), pesos 3, 1, 3, 1...
  function eanValido(codigo) {
    if (!/^(\d{8}|\d{13})$/.test(codigo)) return false;
    const digitos = codigo.split("").map(Number);
    const verificador = digitos.pop();
    const soma = digitos.reverse().reduce((total, d, i) => total + d * (i % 2 === 0 ? 3 : 1), 0);
    return (10 - (soma % 10)) % 10 === verificador;
  }

  // Só os números; o UPC-A (12 dígitos, produtos importados) vira EAN-13 com um zero na frente.
  function normalizar(texto) {
    let codigo = String(texto || "").replace(/\D/g, "");
    if (codigo.length === 12) codigo = "0" + codigo;
    return eanValido(codigo) ? codigo : null;
  }

  // ---------- Quem lê o código ----------
  function carregarZxing() {
    if (!estado.zxing) {
      estado.zxing = new Promise((pronto, falhou) => {
        if (window.ZXingBrowser) return pronto();
        const script = document.createElement("script");
        script.src = ZXING;
        script.crossOrigin = "anonymous";
        script.onload = () => pronto();
        script.onerror = () => {
          estado.zxing = null; // tenta de novo na próxima vez
          falhou(new Error("não carregou " + ZXING));
        };
        document.head.appendChild(script);
      });
    }
    return estado.zxing;
  }

  // Devolve uma função que recebe o vídeo e devolve os códigos que achou no quadro atual.
  async function criarDetector() {
    if ("BarcodeDetector" in window) {
      try {
        const suportados = await window.BarcodeDetector.getSupportedFormats();
        const formatos = ["ean_13", "ean_8", "upc_a"].filter((f) => suportados.includes(f));
        if (formatos.includes("ean_13")) {
          const detector = new window.BarcodeDetector({ formats: formatos });
          console.info("[ean] lendo com o BarcodeDetector do navegador");
          return async (video) => (await detector.detect(video)).map((c) => c.rawValue);
        }
      } catch (erro) {
        console.warn("[ean] BarcodeDetector indisponível; usando a ZXing", erro);
      }
    }
    await carregarZxing();
    const Z = window.ZXingBrowser;
    const leitor = new Z.BrowserMultiFormatReader(new Map());
    leitor.possibleFormats = [Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8, Z.BarcodeFormat.UPC_A];
    const quadro = document.createElement("canvas");
    const pincel = quadro.getContext("2d", { willReadFrequently: true });
    console.info("[ean] lendo com a biblioteca ZXing");
    return async (video) => {
      if (!video.videoWidth) return [];
      const escala = Math.min(1, 1280 / video.videoWidth); // quadro grande deixa a leitura lenta
      quadro.width = Math.round(video.videoWidth * escala);
      quadro.height = Math.round(video.videoHeight * escala);
      pincel.drawImage(video, 0, 0, quadro.width, quadro.height);
      try {
        return [leitor.decodeFromCanvas(quadro).getText()];
      } catch (erro) {
        return []; // nenhum código neste quadro
      }
    };
  }

  // ---------- Janela ----------
  function elemento(tag, classe, texto) {
    const el = document.createElement(tag);
    if (classe) el.className = classe;
    if (texto) el.textContent = texto;
    return el;
  }

  function montarJanela() {
    const janela = elemento("div", "ean-janela");
    janela.setAttribute("role", "dialog");
    janela.setAttribute("aria-modal", "true");
    janela.setAttribute("aria-label", "Ler código de barras");
    const caixa = elemento("div", "ean-caixa");

    const camera = elemento("div", "ean-camera");
    const video = elemento("video", "ean-video");
    video.muted = true;
    video.autoplay = true;
    video.setAttribute("playsinline", ""); // no iPhone, sem isso o vídeo abre em tela cheia
    const guia = elemento("div", "ean-guia");
    ["c1", "c2", "c3", "c4"].forEach((c) => guia.appendChild(elemento("span", "ean-canto " + c)));
    guia.appendChild(elemento("div", "ean-linha"));
    camera.append(video, guia);

    const instrucao = elemento("p", "ean-instrucao", "Aponte para o código de barras");

    // Para digitar o código (aparece quando não há câmera).
    const digitar = elemento("form", "ean-digitar");
    digitar.hidden = true;
    const motivo = elemento("p", "ean-motivo");
    const linha = elemento("div", "ean-linha-digitar");
    const campo = elemento("input", "ean-campo");
    campo.type = "text";
    campo.inputMode = "numeric";
    campo.autocomplete = "off";
    campo.maxLength = 16;
    campo.placeholder = "Ex.: 7891234567895";
    campo.setAttribute("aria-label", "Código de barras");
    const buscar = elemento("button", "ean-buscar", "Buscar");
    buscar.type = "submit";
    linha.append(campo, buscar);
    const erro = elemento("p", "ean-erro");
    erro.setAttribute("role", "alert");
    digitar.append(motivo, linha, erro);
    digitar.addEventListener("submit", (evento) => {
      evento.preventDefault();
      const ean = normalizar(campo.value);
      const tamanho = campo.value.replace(/\D/g, "").length;
      if (ean) lido(ean);
      else if (tamanho === 8 || tamanho === 13) erro.textContent = "Código inválido: algum número não confere. Confira e tente de novo.";
      else erro.textContent = "Código inválido: são 8 ou 13 números. Confira e tente de novo.";
    });

    const fechar = elemento("button", "ean-fechar", "Fechar");
    fechar.type = "button";
    fechar.addEventListener("click", fecharJanela);

    caixa.append(camera, instrucao, digitar, fechar);
    janela.appendChild(caixa);
    return { janela, camera, video, instrucao, digitar, motivo, campo, fechar };
  }

  function semCamera(texto) {
    const j = estado.janela;
    if (!j) return;
    pararCamera();
    j.camera.hidden = true;
    j.instrucao.hidden = true;
    j.motivo.textContent = texto;
    j.digitar.hidden = false;
    j.campo.focus();
  }

  async function abrirJanela() {
    if (estado.aberto) return;
    const caixa = document.querySelector('[data-testid="stChatInputTextArea"]');
    if (caixa && caixa.disabled) {
      if (window.farmazidsVoz) window.farmazidsVoz.avisar("Aguarde a resposta e tente de novo.");
      return;
    }
    estado.aberto = true;
    estado.janela = montarJanela();
    document.body.appendChild(estado.janela.janela);
    document.documentElement.classList.add("ean-aberto");
    estado.janela.fechar.focus();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      semCamera("Este navegador não dá acesso à câmera. Digite o código de barras:");
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
    } catch (falha) {
      console.warn("[ean] câmera indisponível:", falha.name);
      if (falha.name === "NotAllowedError" || falha.name === "SecurityError") {
        semCamera("A câmera está bloqueada. Para liberar, clique no cadeado ao lado do endereço do site. " +
                  "Ou digite o código de barras:");
      } else if (falha.name === "NotFoundError" || falha.name === "OverconstrainedError") {
        semCamera("Não encontrei uma câmera neste aparelho. Digite o código de barras:");
      } else {
        semCamera("Não consegui abrir a câmera. Digite o código de barras:");
      }
      return;
    }
    if (!estado.aberto) { // fechou enquanto o navegador pedia a permissão
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    estado.stream = stream;
    const video = estado.janela.video;
    video.srcObject = stream;
    video.play().catch(() => {});

    let detectar;
    try {
      detectar = await criarDetector();
    } catch (falha) {
      console.warn("[ean] leitor indisponível", falha);
      semCamera("Não consegui carregar o leitor de código de barras. Digite o código:");
      return;
    }
    if (estado.aberto) procurar(detectar, video);
  }

  function procurar(detectar, video) {
    estado.timer = setTimeout(async () => {
      if (!estado.aberto || !estado.stream) return;
      let codigos = [];
      try {
        codigos = await detectar(video);
      } catch (erro) {
        codigos = [];
      }
      const ean = codigos.map(normalizar).find(Boolean);
      if (ean) lido(ean);
      else procurar(detectar, video);
    }, INTERVALO);
  }

  function pararCamera() {
    clearTimeout(estado.timer);
    estado.timer = null;
    if (estado.stream) estado.stream.getTracks().forEach((t) => t.stop());
    estado.stream = null;
  }

  function fecharJanela() {
    pararCamera();
    estado.aberto = false;
    if (estado.janela) estado.janela.janela.remove();
    estado.janela = null;
    document.documentElement.classList.remove("ean-aberto");
    const botao = document.querySelector(".ean-botao");
    if (botao) botao.focus();
  }

  function lido(ean) {
    console.info("[ean] código lido:", ean);
    fecharJanela();
    if (window.farmazidsVoz && window.farmazidsVoz.perguntar) window.farmazidsVoz.perguntar(PERGUNTA + ean);
  }

  document.addEventListener("click", (evento) => {
    const botao = evento.target.closest && evento.target.closest(".ean-botao");
    if (!botao) return;
    evento.preventDefault();
    abrirJanela();
  });
  document.addEventListener("keydown", (evento) => {
    if (evento.key === "Escape" && estado.aberto) fecharJanela();
  });
  // Trocou de aba ou bloqueou o celular: desliga a câmera.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && estado.aberto) fecharJanela();
  });

  window.farmazidsEan.eanValido = eanValido; // para conferir no console
})();
