(function () {
  const invalidMessage =
    "Please edit the Capstone Project title. Use full words only with letters, numbers, spaces, commas, and hyphens. Acronyms, abbreviations, and other special characters are not allowed.";
  const titleLengthMessage = "Capstone Project title must be 15 words or fewer.";
  const commonAcronyms = new Set([
    "ai",
    "api",
    "4ir",
    "b2b",
    "b2c",
    "bbbee",
    "bee",
    "ceo",
    "cfo",
    "covid",
    "covid19",
    "crm",
    "dept",
    "erp",
    "esg",
    "fin",
    "govt",
    "hr",
    "ict",
    "info",
    "intl",
    "it",
    "jbs",
    "jse",
    "kpi",
    "mba",
    "mgmt",
    "mgt",
    "ngo",
    "npo",
    "ops",
    "org",
    "popia",
    "roi",
    "sa",
    "sars",
    "sme",
    "smes",
    "uj",
    "uk",
    "usa",
    "vs",
  ]);

  function hasAcronymOrAbbreviation(word) {
    return word.split(/[,-]+/).some((part) => {
      const letters = part.replace(/[^A-Za-z]/g, "");
      if (commonAcronyms.has(part.toLowerCase())) return true;
      const uppercaseCount = Array.from(letters).filter((char) => /[A-Z]/.test(char)).length;
      return letters.length > 1 && uppercaseCount >= 2;
    });
  }

  function validationError(value) {
    const normalized = String(value || "").trim().replace(/\s+/g, " ");
    if (!normalized) return "";
    if (/[^A-Za-z0-9\s,-]/.test(normalized)) return invalidMessage;
    if (normalized.split(" ").some(hasAcronymOrAbbreviation)) return invalidMessage;
    const wordCount = normalized.split(" ").filter(Boolean).length;
    if (wordCount > 15) return titleLengthMessage;
    return "";
  }

  function capitalizeWord(word) {
    return word
      .split("-")
      .map((part) => {
        const lowered = part.toLowerCase();
        for (let index = 0; index < lowered.length; index += 1) {
          if (/[A-Za-z]/.test(lowered[index])) {
            return lowered.slice(0, index) + lowered[index].toUpperCase() + lowered.slice(index + 1);
          }
        }
        return lowered;
      })
      .join("-");
  }

  function formatProjectTitle(value) {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ")
      .split(" ")
      .filter(Boolean)
      .map(capitalizeWord)
      .join(" ");
  }

  document.querySelectorAll("[data-project-title-input]").forEach((input) => {
    const validateInput = () => {
      const error = validationError(input.value);
      input.setCustomValidity(error);
      return !error;
    };

    input.addEventListener("input", validateInput);
    input.addEventListener("blur", () => {
      if (validateInput()) {
        input.value = formatProjectTitle(input.value);
      } else {
        input.reportValidity();
      }
    });
    input.addEventListener("change", () => {
      if (validateInput()) {
        input.value = formatProjectTitle(input.value);
      }
    });
  });
})();
