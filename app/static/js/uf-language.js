(function () {
    const ASSAMESE_MAP = {
        "Dashboard": "ডেছব’ৰ্ড",
        "Profile": "প্ৰ’ফাইল",
        "My Profile": "মোৰ প্ৰ’ফাইল",
        "Logout": "লগআউট",
        "Login": "লগইন",
        "Submit": "জমা দিয়ক",
        "Save": "সংৰক্ষণ কৰক",
        "Update": "আপডেট কৰক",
        "Cancel": "বাতিল কৰক",
        "Back": "উভতি যাওক",
        "Next": "পিছলৈ যাওক",
        "Search": "সন্ধান কৰক",
        "Reset": "ৰিছেট কৰক",
        "View": "চাওক",
        "Edit": "সম্পাদনা কৰক",
        "Delete": "মচি পেলাওক",
        "Download": "ডাউনলোড কৰক",
        "Upload": "আপলোড কৰক",
        "Status": "স্থিতি",
        "Action": "কাৰ্য",
        "Actions": "কাৰ্যসমূহ",

        "Products": "সামগ্ৰীসমূহ",
        "All Products": "সকলো সামগ্ৰী",
        "Farmer Products": "কৃষকৰ সামগ্ৰী",
        "Add Product": "সামগ্ৰী যোগ কৰক",
        "Product Name": "সামগ্ৰীৰ নাম",
        "Category": "শ্ৰেণী",
        "Type": "প্ৰকাৰ",
        "Price": "মূল্য",
        "Quantity": "পৰিমাণ",
        "Available Quantity": "উপলব্ধ পৰিমাণ",
        "Unit Price": "একক মূল্য",
        "Variety": "জাত",
        "Average Size": "গড় আকাৰ",

        "Orders": "অৰ্ডাৰসমূহ",
        "All Orders": "সকলো অৰ্ডাৰ",
        "Order Now": "এতিয়া অৰ্ডাৰ কৰক",
        "Buy": "ক্ৰয়",
        "Sell": "বিক্ৰী",
        "Purchases": "ক্ৰয়সমূহ",
        "Transactions": "লেনদেনসমূহ",
        "Invoice": "চালান",

        "Notifications": "জাননীসমূহ",
        "Support": "সহায়",
        "LMS": "এল এম এছ",
        "Finance": "বিত্ত",
        "Insurance": "বীমা",
        "Consult a Specialist": "বিশেষজ্ঞৰ পৰামৰ্শ লওক",

        "Centre": "কেন্দ্ৰ",
        "Center": "কেন্দ্ৰ",
        "Centre UID": "কেন্দ্ৰ UID",
        "Center UID": "কেন্দ্ৰ UID",
        "Mitra": "মিত্ৰ",
        "Mitra UID": "মিত্ৰ UID",
        "Farmer": "কৃষক",
        "Farmer Name": "কৃষকৰ নাম",
        "Farmer Contact": "কৃষকৰ যোগাযোগ",

        "State": "ৰাজ্য",
        "District": "জিলা",
        "Block": "খণ্ড",
        "Village": "গাঁও",
        "Contact Number": "যোগাযোগ নম্বৰ",
        "Phone Number": "ফোন নম্বৰ",
        "Email": "ইমেইল",
        "Username": "ব্যৱহাৰকাৰীৰ নাম",
        "Password": "পাছৱৰ্ড",

        "Welcome": "স্বাগতম",
        "Total Products": "মুঠ সামগ্ৰী",
        "Total Orders": "মুঠ অৰ্ডাৰ",
        "Total Sales": "মুঠ বিক্ৰী",
        "Total Purchases": "মুঠ ক্ৰয়",
        "Pending": "বাকী আছে",
        "Approved": "অনুমোদিত",
        "Rejected": "নাকচ কৰা হৈছে",
        "Active": "সক্ৰিয়",
        "Inactive": "নিষ্ক্ৰিয়",

        "No records found": "কোনো তথ্য পোৱা নগ’ল",
        "Select Option": "বিকল্প বাছনি কৰক",
        "Select Centre": "কেন্দ্ৰ বাছনি কৰক",
        "Select Center": "কেন্দ্ৰ বাছনি কৰক",
        "Select Mitra": "মিত্ৰ বাছনি কৰক",
        "Select Category": "শ্ৰেণী বাছনি কৰক",

        "Created At": "সৃষ্টি কৰা তাৰিখ",
        "Updated At": "আপডেট কৰা তাৰিখ"
    };

    const SKIP_TAGS = ["SCRIPT", "STYLE", "NOSCRIPT", "INPUT", "TEXTAREA", "SELECT", "OPTION"];

    function shouldSkipNode(node) {
        if (!node || !node.parentElement) return true;

        const parent = node.parentElement;

        if (SKIP_TAGS.includes(parent.tagName)) return true;
        if (parent.closest("[data-no-translate]")) return true;

        /*
          Skip table data values to avoid translating database values:
          farmer names, product names, centre IDs, quantities, etc.
          Table headings will still be translated.
        */
        if (parent.closest("td")) return true;

        return false;
    }

    function saveOriginal(node) {
        if (!node.parentElement.dataset.originalText) {
            node.parentElement.dataset.originalText = node.nodeValue;
        }
    }

    function translateTextNode(node) {
        if (shouldSkipNode(node)) return;

        const originalText = node.parentElement.dataset.originalText || node.nodeValue;
        const trimmed = originalText.trim();

        if (!trimmed) return;

        saveOriginal(node);

        if (ASSAMESE_MAP[trimmed]) {
            node.nodeValue = originalText.replace(trimmed, ASSAMESE_MAP[trimmed]);
        }
    }

    function restoreTextNode(node) {
        if (!node || !node.parentElement) return;

        const originalText = node.parentElement.dataset.originalText;
        if (originalText) {
            node.nodeValue = originalText;
        }
    }

    function walkTextNodes(callback) {
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            {
                acceptNode: function (node) {
                    if (!node.nodeValue.trim()) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    return NodeFilter.FILTER_ACCEPT;
                }
            }
        );

        const nodes = [];
        while (walker.nextNode()) {
            nodes.push(walker.currentNode);
        }

        nodes.forEach(callback);
    }

    function translatePlaceholdersToAssamese() {
        document.querySelectorAll("input[placeholder], textarea[placeholder]").forEach(function (el) {
            if (!el.dataset.originalPlaceholder) {
                el.dataset.originalPlaceholder = el.getAttribute("placeholder") || "";
            }

            const original = el.dataset.originalPlaceholder.trim();
            if (ASSAMESE_MAP[original]) {
                el.setAttribute("placeholder", ASSAMESE_MAP[original]);
            }
        });
    }

    function restorePlaceholders() {
        document.querySelectorAll("[data-original-placeholder]").forEach(function (el) {
            el.setAttribute("placeholder", el.dataset.originalPlaceholder);
        });
    }

    function setButtonState(lang) {
        const enBtn = document.getElementById("ufLangEnglish");
        const asBtn = document.getElementById("ufLangAssamese");

        if (enBtn) enBtn.classList.toggle("active", lang === "en");
        if (asBtn) asBtn.classList.toggle("active", lang === "as");
    }

    function changeLanguage(lang) {
        if (lang === "as") {
            walkTextNodes(translateTextNode);
            translatePlaceholdersToAssamese();
        } else {
            walkTextNodes(restoreTextNode);
            restorePlaceholders();
        }

        localStorage.setItem("uf_language", lang);
        setButtonState(lang);
    }

    document.addEventListener("DOMContentLoaded", function () {
        const enBtn = document.getElementById("ufLangEnglish");
        const asBtn = document.getElementById("ufLangAssamese");

        if (enBtn) {
            enBtn.addEventListener("click", function () {
                changeLanguage("en");
            });
        }

        if (asBtn) {
            asBtn.addEventListener("click", function () {
                changeLanguage("as");
            });
        }

        setButtonState(localStorage.getItem("uf_language") || "en");
    });
})();