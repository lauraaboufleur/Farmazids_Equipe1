// Voz no chatbot da Farmazids, só com recursos do navegador (sem AWS e sem custo):
// - pesquisa por voz: botão de microfone na caixa de pergunta (reconhecimento de fala);
// - leitura da resposta: botões "Ouvir" no rodapé das respostas (síntese de fala).
// O app.py carrega este arquivo a cada execução e troca __CONFIG__ pela configuração
// da voz (VOZ, no app.py). Ele só se instala uma vez por página.
(function () {
  if (window.farmazidsVoz) return;
  const CONFIG = __CONFIG__;
  const Reconhecimento = window.SpeechRecognition || window.webkitSpeechRecognition;
  const sintese = window.speechSynthesis;
  const estado = {
    lidas: new Set(),     // respostas que já existiam (não são lidas sozinhas)
    idLendo: null,        // resposta que está sendo lida agora
    ouvindo: false,       // microfone ligado
    porVoz: false,        // a última pergunta foi feita por voz: ler a resposta sozinha
  };
  window.farmazidsVoz = estado;

  // O sinal de menor vai escrito como \x3c: se o texto do script tiver esse sinal seguido de
  // letra ou barra, o Streamlit (DOMPurify) apaga o script inteiro.
  const MIC = '\x3csvg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '\x3crect x="9" y="3" width="6" height="11" rx="3"/>\x3cpath d="M5 11a7 7 0 0 0 14 0"/>' +
    '\x3cline x1="12" y1="18" x2="12" y2="21"/>\x3c/svg>';

  // ---------- Aviso curto (microfone bloqueado, não ouviu nada...) ----------
  function avisar(texto) {
    document.querySelectorAll(".voz-aviso").forEach((a) => a.remove());
    const aviso = document.createElement("div");
    aviso.className = "voz-aviso";
    aviso.setAttribute("role", "status");
    aviso.textContent = texto;
    document.body.appendChild(aviso);
    setTimeout(() => aviso.remove(), 8000);
  }

  // ---------- Leitura da resposta ----------
  let vozEscolhida = null;
  function escolherVoz() {
    const vozes = sintese.getVoices();
    for (const nome of CONFIG.vozes) {
      const voz = vozes.find((v) => v.name.toLowerCase().includes(nome.toLowerCase()));
      if (voz) return voz;
    }
    const idioma = CONFIG.idioma.toLowerCase();
    return vozes.find((v) => v.lang.replace("_", "-").toLowerCase() === idioma) || null;
  }

  function botaoDa(id) {
    return document.querySelector('.voz-ouvir[data-id="' + id + '"]');
  }

  function marcar(id, lendo) {
    const botao = botaoDa(id);
    const rotulo = lendo ? "⏹ Parar" : "🔊 Ouvir";
    // Só mexe se mudou: cada mudança acorda o observador da página lá embaixo.
    if (!botao || botao.textContent === rotulo) return;
    botao.textContent = rotulo;
    botao.classList.toggle("lendo", lendo);
  }

  function pararLeitura() {
    if (!sintese) return;
    sintese.cancel();
    if (estado.idLendo !== null) marcar(estado.idLendo, false);
    estado.idLendo = null;
  }

  function ler(botao) {
    const texto = botao.dataset.fala;
    if (!sintese || !texto) return;
    pararLeitura();
    const fala = new SpeechSynthesisUtterance(texto);
    fala.lang = CONFIG.idioma;
    fala.rate = CONFIG.velocidade;
    fala.pitch = CONFIG.tom;
    vozEscolhida = vozEscolhida || escolherVoz();
    if (vozEscolhida) fala.voice = vozEscolhida;
    const id = botao.dataset.id;
    fala.onend = fala.onerror = () => {
      if (estado.idLendo === id) {
        marcar(id, false);
        estado.idLendo = null;
      }
    };
    estado.idLendo = id;
    marcar(id, true);
    sintese.speak(fala);
  }

  if (sintese) {
    document.documentElement.classList.add("voz-tts"); // sem isso, os botões "Ouvir" ficam escondidos
    vozEscolhida = escolherVoz();
    sintese.onvoiceschanged = () => { vozEscolhida = escolherVoz(); };
  }

  document.addEventListener("click", (evento) => {
    const botao = evento.target.closest && evento.target.closest(".voz-ouvir");
    if (!botao) return;
    evento.preventDefault();
    if (estado.idLendo === botao.dataset.id) pararLeitura();
    else ler(botao);
  });

  // ---------- Pesquisa por voz ----------
  function preencher(texto) {
    const caixa = document.querySelector('[data-testid="stChatInputTextArea"]');
    if (!caixa) return;
    // O Streamlit (React) só percebe o texto novo com o setter nativo + evento "input".
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(caixa, texto);
    caixa.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function enviar(tentativas) {
    const botao = document.querySelector('[data-testid="stChatInputSubmitButton"]');
    if (botao && !botao.disabled) botao.click();
    else if (tentativas > 0) setTimeout(() => enviar(tentativas - 1), 100);
  }

  function marcarMic(ouvindo) {
    estado.ouvindo = ouvindo;
    document.querySelectorAll(".voz-mic").forEach((b) => {
      b.classList.toggle("ouvindo", ouvindo);
      b.setAttribute("aria-pressed", ouvindo ? "true" : "false");
    });
  }

  let reconhecimento = null;
  function ouvir() {
    if (estado.ouvindo) {
      reconhecimento.stop(); // segundo toque: para de ouvir (e envia o que já ouviu)
      return;
    }
    pararLeitura();
    reconhecimento = new Reconhecimento();
    reconhecimento.lang = CONFIG.idioma;
    reconhecimento.interimResults = true; // o texto aparece enquanto a pessoa fala
    reconhecimento.continuous = false;    // ao parar de falar, termina
    let texto = "";
    reconhecimento.onstart = () => marcarMic(true);
    reconhecimento.onresult = (evento) => {
      texto = Array.from(evento.results).map((r) => r[0].transcript).join("");
      preencher(texto);
    };
    reconhecimento.onerror = (evento) => {
      texto = "";
      if (evento.error === "not-allowed" || evento.error === "service-not-allowed") {
        avisar("O microfone está bloqueado. Para liberar, clique no cadeado ao lado do endereço do site, " +
               "permita o microfone e toque no botão de novo.");
      } else if (evento.error === "no-speech") {
        avisar("Não ouvi nada. Toque no microfone e fale a sua pergunta.");
      } else if (evento.error === "network") {
        avisar("Sem internet para reconhecer a fala. Digite a pergunta, por favor.");
      } else if (evento.error !== "aborted") {
        avisar("Não consegui ouvir. Tente de novo ou digite a pergunta.");
      }
    };
    reconhecimento.onend = () => {
      marcarMic(false);
      if (texto.trim()) {
        estado.porVoz = true;
        enviar(10);
      }
    };
    try {
      reconhecimento.start();
    } catch (erro) {
      marcarMic(false);
    }
  }

  function colocarMic() {
    const caixa = document.querySelector('[data-testid="stChatInput"]');
    if (!caixa || caixa.querySelector(".voz-mic")) return;
    const botao = document.createElement("button");
    botao.type = "button";
    botao.className = "voz-mic" + (estado.ouvindo ? " ouvindo" : "");
    botao.title = "Perguntar por voz";
    botao.setAttribute("aria-label", "Perguntar por voz");
    botao.innerHTML = MIC;
    botao.addEventListener("click", (evento) => {
      evento.preventDefault();
      ouvir();
    });
    caixa.appendChild(botao);
  }

  // ---------- A cada mudança da página (o Streamlit redesenha tudo a cada interação) ----------
  function respostasNovas() {
    document.querySelectorAll(".voz-ouvir").forEach((botao) => {
      const id = botao.dataset.id;
      if (estado.lidas.has(id)) return;
      estado.lidas.add(id);
      if (estado.porVoz) {
        estado.porVoz = false;
        ler(botao); // pergunta feita por voz: a resposta é lida sozinha
      }
    });
    if (estado.idLendo !== null) marcar(estado.idLendo, true); // o botão redesenhado volta a dizer "Parar"
  }

  document.querySelectorAll(".voz-ouvir").forEach((b) => estado.lidas.add(b.dataset.id));
  let agendado = false;
  function atualizar() {
    agendado = false;
    if (Reconhecimento) colocarMic();
    respostasNovas();
  }
  new MutationObserver(() => {
    if (!agendado) {
      agendado = true;
      requestAnimationFrame(atualizar);
    }
  }).observe(document.body, { childList: true, subtree: true });
  atualizar();
})();
