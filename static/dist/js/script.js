let conversationId = null;
// Crée un nouveau bloc de message à partir du modèle HTML caché.
function createMessageElement() {
    // Récupère le conteneur qui affiche les messages de la conversation.
    const output = document.querySelector("#gpt-output");

    // Récupère le bloc HTML utilisé comme modèle.
    const template = document.querySelector("#chat-template");

    // Copie le modèle avec tous ses éléments enfants.
    const clone = template.cloneNode(true);

    // Retire l’identifiant du clone pour éviter les identifiants en double.
    clone.id = "";

    // Ajoute le nouveau bloc à la fin de la conversation.
    output.appendChild(clone);

    // Retire la classe Tailwind "hidden" pour rendre le bloc visible.
    clone.classList.remove("hidden");

    // Retourne l’élément qui recevra le texte du message.
    return clone.querySelector(".message");
}

// Ajoute un message à la conversation et retourne son élément HTML.
function appendMessage(message) {
    // Crée un bloc visible et récupère son élément ".message".
    const infoBlock = createMessageElement();

    //si _cloneAnswerBlock() retourne null
    // Vérifie que l’élément destiné au message existe.
    if (!infoBlock) {
        // Affiche une erreur dans la console du navigateur.
        console.error("Failed to create the information block");

        // Arrête la fonction en indiquant qu’aucun élément n’est disponible.
        return null;
    }

    // Insère le message comme du texte, sans l’interpréter comme du HTML.
    infoBlock.innerText = message;

    // Retourne l’élément pour pouvoir modifier son contenu ensuite.
    return infoBlock;
}

// Récupère les messages affichés dans la conversation.
function getChatHistory() {
    // Sélectionne les éléments ".message", sauf celui du modèle caché .
    const infoBlocks = document.querySelectorAll(
        ".message:not(#chat-template .message)"
    );

    // Vérifie si aucun message n’a été trouvé.
    if (!infoBlocks.length) {
        // Affiche un avertissement dans la console du navigateur.
        console.warn("Aucun bloc d'information trouvé");

        // Retourne un tableau vide si la conversation ne contient aucun message.
        return [];
    }

    // Convertit la liste d’éléments en tableau, puis récupère le HTML de chacun.
    // Attention : innerHTML récupère aussi les balises HTML. Pour récupérer uniquement le texte, utilise block.textContent.
    return Array.from(infoBlocks).map(block => block.innerText);
}

// Envoie la conversation à Flask et retourne un lecteur de la réponse progressive.
async function fetchPromptResponse(prompt) {
    if (!conversationId) {
    const startResponse = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            domain: document.querySelector("#domain").value
        })
    });

    const conversation = await startResponse.json();
    if (!startResponse.ok) {
        throw new Error(conversation.error || "Could not start a conversation.");
    }

    conversationId = conversation.id;
}
    // Envoie une requête à la route Flask "/prompt" et attend sa réponse initiale.
    const response = await fetch("/prompt", {
        // Utilise POST pour transmettre les données au serveur.
        method: "POST",

        // Indique que le corps de la requête contient du JSON.
        headers: {
            "Content-Type": "application/json",
        },

        // Récupère l’historique et le convertit en texte JSON.
    body: JSON.stringify({
       messages: getChatHistory(),
       domain: document.querySelector("#domain").value
    }),
    });

    // Retourne un lecteur permettant de lire la réponse fragment par fragment.
    return response.body.getReader();
}

// Lit la réponse progressive et affiche le texte converti en HTML.
async function readResponseChunks(reader, gptOutput) {
    // Décode les octets reçus en texte UTF-8.
    const decoder = new TextDecoder("utf-8");

    // Crée le convertisseur Markdown vers HTML.
    const converter = new showdown.Converter();

    // Conserve le texte complet reçu jusqu’à présent.
    let text = "";

    try {
        while (true) {
            // Attend le prochain fragment.
            // done indique la fin du flux ; value contient les octets reçus.
            const { done, value } = await reader.read();

            // Quitte la boucle lorsque le flux est terminé.
            if (done) break;

            // Décode le fragment en conservant les caractères incomplets
            // pour les compléter lors de la réception suivante.
            text += decoder.decode(value, { stream: true });

            // Convertit le Markdown en HTML et sécurise le résultat.
            gptOutput.innerHTML = DOMPurify.sanitize(
                converter.makeHtml(text)
            );
        }

        // Termine le décodage des éventuels octets restants.
        text += decoder.decode();

        // Affiche le résultat final.
        gptOutput.innerHTML = DOMPurify.sanitize(
            converter.makeHtml(text)
        );
    } finally {
        // Libère le lecteur, même si une erreur survient.
        reader.releaseLock();
    }
}

// Attend que le HTML soit chargé avant d’exécuter ce code.
document.addEventListener("DOMContentLoaded", () => {
    // Récupère le formulaire grâce à son identifiant.
    const form = document.querySelector("#prompt-form");

    // Récupère le bouton situé dans le formulaire.
    const button = form.querySelector("button");

    // Exécute cette fonction lors de l’envoi du formulaire.
    // async permet d’utiliser await pour attendre les opérations asynchrones.
    form.addEventListener("submit", async (event) => {
        // Empêche l’envoi classique du formulaire et le rechargement de la page.
        event.preventDefault();

        // Lit la question et retire les espaces au début et à la fin.
        const prompt = form.elements.prompt.value.trim();

        // Arrête si la question est vide ou si une réponse est déjà en cours.
        if (!prompt || button.disabled) return;

        // Affiche la question de l’utilisateur dans la conversation.
        appendMessage(prompt);

        // Remet les champs du formulaire à leurs valeurs initiales.
        form.reset();

        // Désactive le bouton pour éviter plusieurs envois simultanés.
        button.disabled = true;

        try {
            // Envoie la conversation à Flask et récupère le lecteur du flux.
            // Cet appel précède la création du bloc vide pour l’exclure de l’historique.
            const reader = await fetchPromptResponse(prompt);

            // Crée un bloc vide destiné à recevoir la réponse de l’IA.
            const output = appendMessage("");

            // Lit et affiche progressivement les fragments de la réponse.
            await readResponseChunks(reader, output);
        } catch (error) {
            // Affiche un message d’erreur dans la conversation.
            //Error: unable to retrieve the response.
            appendMessage("Erreur : impossible de récupérer la réponse.");

            // Affiche les détails de l’erreur dans la console du navigateur.
            console.error(error);
        } finally {
            // Réactive le bouton après la réussite ou l’échec du traitement.
            button.disabled = false;
        }
    });
});