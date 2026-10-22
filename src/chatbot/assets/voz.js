// Voz no chatbot da Farmazids:
// - pesquisa por voz: botão de microfone na caixa de pergunta (reconhecimento de fala);
// - leitura da resposta: botões "Ouvir" no rodapé das respostas (nada é lido sozinho, nem
//   quando a pergunta foi feita por voz). O áudio do Amazon Polly
//   só é gerado quando vai ser ouvido: o "Ouvir" clica num botão escondido da resposta
//   (.st-key-pedir-audio-N), o app.py gera o áudio e o põe num player escondido
//   (.st-key-audio-N), e ele é tocado aqui. Se o Polly falhar, demorar demais ou o
//   áudio não tocar, usa a voz do navegador (síntese de fala), frase por frase, com a
//   voz escolhida pela tabela de prioridade do app.py (VOZ).
// Também expõe perguntar(texto) e avisar(texto) em window.farmazidsVoz, para o leitor de
// código de barras (assets/leitor_ean.js) usar o mesmo caminho, e cuida dos botões com uma
// pergunta pronta (atalhos e oferta) e do ☰ que abre a coluna de atalhos.
// O app.py carrega este arquivo a cada execução e troca __CONFIG__ pela configuração
// da voz (VOZ, no app.py). Ele só se instala uma vez por página.
(function () {
  if (window.farmazidsVoz) return;
  const CONFIG = __CONFIG__;
  const Reconhecimento = window.SpeechRecognition || window.webkitSpeechRecognition;
  const sintese = window.speechSynthesis;
  const estado = {
    idLendo: null,        // resposta que está sendo lida agora
    audio: null,          // áudio do Polly tocando agora
    esperando: null,      // resposta cujo áudio foi pedido ao app e ainda não chegou
    leitura: 0,           // muda a cada leitura na voz do navegador: a antiga para de falar
    pausa: null,          // pausa entre duas frases da voz do navegador
    ouvindo: false,       // microfone ligado
  };
  window.farmazidsVoz = estado;

  // O sinal de menor vai escrito como \x3c: se o texto do script tiver esse sinal seguido de
  // letra ou barra, o Streamlit (DOMPurify) apaga o script inteiro.
  const RAIO = '\x3csvg viewBox="0 0 24 24" width="17" height="17" fill="currentColor" aria-hidden="true">' +
    '\x3cpath d="M13.5 2 4.5 13.5h6L9.5 22l9-11.5h-6z"/>\x3c/svg>';
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
  // Voz do navegador: a primeira da tabela de prioridade (VOZ, no app.py) que existir no
  // aparelho, só entre as pt-BR; senão, a pt-BR padrão do sistema. Vem com a velocidade e
  // o tom daquela voz.
  function escolherVoz() {
    const idioma = CONFIG.idioma.toLowerCase();
    const vozes = sintese.getVoices().filter((v) => v.lang.replace("_", "-").toLowerCase() === idioma);
    for (const preferida of CONFIG.vozes) {
      const voz = vozes.find((v) => v.name.toLowerCase().includes(preferida.nome.toLowerCase()));
      if (voz) return { voz: voz, velocidade: preferida.velocidade, tom: preferida.tom };
    }
    const padrao = vozes.find((v) => v.default) || vozes.find((v) => v.localService) || vozes[0] || null;
    return { voz: padrao, velocidade: CONFIG.padrao.velocidade, tom: CONFIG.padrao.tom };
  }

  // Frase por frase: com a pausa entre elas, a voz do navegador soa menos corrida (e o
  // Chrome não corta leituras longas).
  function frasesDe(texto) {
    return (texto.match(/[^.!?;\n]+[.!?;]*/g) || []).map((f) => f.trim()).filter((f) => /\w/.test(f));
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
    estado.esperando = null;
    estado.leitura += 1;
    clearTimeout(estado.pausa);
    if (estado.audio) {
      estado.audio.pause();
      estado.audio.currentTime = 0;
      estado.audio = null;
    }
    if (sintese) sintese.cancel();
    if (estado.idLendo !== null) marcar(estado.idLendo, false);
    estado.idLendo = null;
  }

  function terminou(id) {
    if (estado.idLendo === id) {
      marcar(id, false);
      estado.idLendo = null;
      estado.audio = null;
    }
  }

  function audioDa(id) {
    return document.querySelector(".st-key-audio-" + id + " audio");
  }

  function tocar(audio, botao, inicio) {
    const id = botao.dataset.id;
    estado.idLendo = id;
    estado.audio = audio;
    marcar(id, true);
    audio.currentTime = 0;
    audio.onended = () => terminou(id);
    audio.play().then(() => {
      const espera = inicio ? " (" + Math.round(performance.now() - inicio) + " ms depois do pedido)" : "";
      console.info("[voz] lendo com o Amazon Polly" + espera);
    }).catch((erro) => {
      console.warn("[voz] o áudio do Polly não tocou; usando a voz do navegador", erro);
      estado.audio = null;
      botao.dataset.origem = "navegador";
      botao.title = "Voz do navegador";
      lerNoNavegador(botao);
    });
  }

  function ler(botao) {
    pararLeitura();
    const id = botao.dataset.id;
    const audio = audioDa(id);
    if (audio) tocar(audio, botao, null);
    else if (botao.dataset.origem === "sob-demanda") pedirAudio(botao);
    else lerNoNavegador(botao);
  }

  // Pede o áudio ao app (botão escondido da resposta) e espera o player chegar.
  function pedirAudio(botao) {
    const id = botao.dataset.id;
    const pedido = document.querySelector(".st-key-pedir-audio-" + id + " button");
    if (!pedido) {
      lerNoNavegador(botao);
      return;
    }
    estado.idLendo = id;
    estado.esperando = id;
    marcar(id, true); // já vira "Parar": um segundo toque cancela o pedido
    pedido.click();
    esperarAudio(id, performance.now(), 80); // até 8 s
  }

  function esperarAudio(id, inicio, tentativas) {
    if (estado.esperando !== id) return; // a pessoa clicou em "Parar"
    const botao = botaoDa(id);
    const audio = audioDa(id);
    if (audio && botao) {
      estado.esperando = null;
      tocar(audio, botao, inicio);
    } else if (botao && (botao.dataset.origem === "navegador" || tentativas <= 0)) {
      estado.esperando = null; // o Polly falhou (o app avisou) ou demorou demais
      lerNoNavegador(botao);
    } else {
      setTimeout(() => esperarAudio(id, inicio, tentativas - 1), 100);
    }
  }

  function lerNoNavegador(botao) {
    const frases = frasesDe(botao.dataset.fala || "");
    if (!sintese || !frases.length) return;
    const escolha = escolherVoz(); // a cada leitura: o navegador carrega as vozes aos poucos
    const id = botao.dataset.id;
    const vez = ++estado.leitura;
    estado.idLendo = id;
    marcar(id, true);
    console.info("[voz] lendo com a voz do navegador: " + (escolha.voz ? escolha.voz.name : "padrão do aparelho") +
      " (velocidade " + escolha.velocidade + ", tom " + escolha.tom + ", " + frases.length + " frases)");
    function falar(i) {
      if (estado.leitura !== vez) return; // parou ou começou outra leitura
      if (i >= frases.length) {
        terminou(id);
        return;
      }
      const fala = new SpeechSynthesisUtterance(frases[i]);
      fala.lang = CONFIG.idioma;
      fala.rate = escolha.velocidade;
      fala.pitch = escolha.tom;
      if (escolha.voz) fala.voice = escolha.voz;
      fala.onend = () => {
        if (estado.leitura === vez) estado.pausa = setTimeout(() => falar(i + 1), CONFIG.pausa_entre_frases);
      };
      fala.onerror = () => {
        if (estado.leitura === vez) terminou(id);
      };
      sintese.speak(fala);
    }
    falar(0);
  }

  // Sem esta classe, os botões "Ouvir" ficam escondidos (CSS). Com o Polly, há áudio
  // mesmo sem a voz do navegador; então ela entra sempre.
  document.documentElement.classList.add("voz-tts");
  if (sintese) sintese.getVoices(); // o Chrome só começa a carregar as vozes quando alguém pede

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

  // O mesmo caminho para quem mais precisar mandar uma pergunta (o leitor_ean.js usa).
  estado.perguntar = (texto) => {
    preencher(texto);
    enviar(10);
  };
  estado.avisar = avisar;

  // Botões que mandam uma pergunta pronta pelo mesmo caminho: atalhos da coluna lateral e a
  // oferta da resposta ("Ver preços de dipirona na São João").
  function fecharColunaNoCelular() {
    const coluna = document.querySelector('[data-testid="stSidebar"]');
    const fechar = document.querySelector('[data-testid="stSidebarCollapseButton"] button');
    if (window.innerWidth <= 768 && coluna && coluna.getAttribute("aria-expanded") === "true" && fechar) fechar.click();
  }

  document.addEventListener("click", (evento) => {
    const alvo = evento.target.closest ? evento.target : null;
    if (!alvo) return;
    // ☰ do cabeçalho: abre a coluna de atalhos (o botão do Streamlit fica escondido com o cabeçalho dele).
    if (alvo.closest(".menu-botao")) {
      evento.preventDefault();
      const abrir = document.querySelector('[data-testid="stExpandSidebarButton"]');
      if (abrir) abrir.click();
      return;
    }
    const botao = alvo.closest("button[data-pergunta]");
    if (!botao) return;
    evento.preventDefault();
    const caixa = document.querySelector('[data-testid="stChatInputTextArea"]');
    if (!caixa || caixa.disabled) {
      avisar("Aguarde a resposta e tente de novo.");
      return;
    }
    fecharColunaNoCelular();
    estado.perguntar(botao.dataset.pergunta);
  });

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
      if (texto.trim()) enviar(10);
    };
    try {
      reconhecimento.start();
    } catch (erro) {
      marcarMic(false);
    }
  }

  // ---------- ⚡ Resposta rápida (dentro da barra de pergunta, à esquerda do microfone) ----------
  // O app.py guarda o modo; aqui o botão só mostra o estado (marcador .modo-estado) e, no clique, aciona
  // o botão escondido que liga e desliga o modo (.st-key-alternar-modo).
  function colocarRapido() {
    const caixa = document.querySelector('[data-testid="stChatInput"]');
    const marcador = document.querySelector(".modo-estado");
    if (!caixa) return;
    let botao = caixa.querySelector(".modo-rapido-botao");
    if (!botao) {
      botao = document.createElement("button");
      botao.type = "button";
      botao.className = "modo-rapido-botao";
      botao.title = "Resposta rápida";
      botao.setAttribute("aria-label", "Resposta rápida");
      botao.innerHTML = RAIO;
      botao.addEventListener("click", (evento) => {
        evento.preventDefault();
        const texto = document.querySelector('[data-testid="stChatInputTextArea"]');
        const alternar = document.querySelector(".st-key-alternar-modo button");
        if (!alternar || alternar.disabled || (texto && texto.disabled)) {
          avisar("Aguarde a resposta e tente de novo.");
          return;
        }
        alternar.click();
      });
      caixa.appendChild(botao);
    }
    const aceso = !!marcador && marcador.dataset.rapido === "1";
    if (botao.classList.contains("aceso") !== aceso) botao.classList.toggle("aceso", aceso);
    if (botao.getAttribute("aria-pressed") !== String(aceso)) botao.setAttribute("aria-pressed", String(aceso));
  }

  // ---------- PDF de uma resposta ----------
  // Marca a resposta (imprimir-esta), abre o que estiver recolhido nela (tabela, detalhes) e chama o
  // "Salvar como PDF" do navegador; o @media print do estilo.css esconde todo o resto.
  function imprimirResposta(botao) {
    // Só uma resposta do chatbot (container com a classe st-key-msg-N-assistant), nunca uma pergunta.
    const resposta = botao.closest('[class*="st-key-msg-"]');
    if (!resposta || !Array.from(resposta.classList).some((c) => /^st-key-msg-.+-assistant$/.test(c))) return;
    document.querySelectorAll(".imprimir-esta").forEach((r) => r.classList.remove("imprimir-esta"));
    resposta.classList.add("imprimir-esta");
    document.documentElement.classList.add("imprimindo");
    const fechados = Array.from(resposta.querySelectorAll("details:not([open])"));
    fechados.forEach((d) => { d.open = true; });
    const terminar = () => {
      window.removeEventListener("afterprint", terminar);
      document.documentElement.classList.remove("imprimindo");
      resposta.classList.remove("imprimir-esta");
      fechados.forEach((d) => { d.open = false; });
    };
    window.addEventListener("afterprint", terminar);
    setTimeout(() => window.print(), 250); // tempo para o que foi aberto se desenhar
  }

  document.addEventListener("click", (evento) => {
    const botao = evento.target.closest && evento.target.closest(".pdf-botao");
    if (!botao) return;
    evento.preventDefault();
    imprimirResposta(botao);
  });

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
  let agendado = false;
  function atualizar() {
    agendado = false;
    if (Reconhecimento) colocarMic();
    colocarRapido();
    if (estado.idLendo !== null) marcar(estado.idLendo, true); // o botão redesenhado volta a dizer "Parar"
  }
  new MutationObserver(() => {
    if (!agendado) {
      agendado = true;
      requestAnimationFrame(atualizar);
    }
  }).observe(document.body, { childList: true, subtree: true });
  atualizar();
})();
