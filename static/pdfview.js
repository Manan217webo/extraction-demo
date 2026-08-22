/* Webo Healthtech — original-document pane.

   Renders the uploaded PDF page by page and draws a box over every region a
   mapped value was read from. Boxes arrive normalised to 0..1 of the page, so
   they survive any zoom or window size without being recalculated.

   Pages render lazily: a placeholder of the right shape goes down immediately and
   the canvas is painted once the page scrolls into view. */

(function () {
  "use strict";

  let readyPromise = null;

  function whenPdfReady() {
    if (readyPromise) return readyPromise;
    readyPromise = new Promise((resolve, reject) => {
      if (window.pdfjsLib) return resolve(window.pdfjsLib);
      const timer = setTimeout(
        () => reject(new Error("The document viewer took too long to load.")),
        15000
      );
      window.addEventListener(
        "pdfjs-ready",
        () => {
          clearTimeout(timer);
          window.pdfjsLib ? resolve(window.pdfjsLib) : reject(new Error("viewer unavailable"));
        },
        { once: true }
      );
    });
    return readyPromise;
  }

  class PdfView {
    constructor(container, options = {}) {
      this.container = container;
      this.onSelect = options.onSelect || (() => {});
      this.doc = null;
      this.pages = new Map(); // page number -> { wrap, canvas, overlay, viewport, rendered }
      this.highlights = [];
      this.focusKey = null;
      this.visible = true;
      this.scale = 1;
      this.observer = null;
      this.renderToken = 0;

      this._onResize = debounce(() => this.relayout(), 180);
      window.addEventListener("resize", this._onResize);
    }

    destroy() {
      window.removeEventListener("resize", this._onResize);
      if (this.observer) this.observer.disconnect();
      if (this.doc) this.doc.destroy().catch(() => {});
      this.doc = null;
      this.pages.clear();
      this.container.innerHTML = "";
    }

    async load(data) {
      const pdfjsLib = await whenPdfReady();
      if (this.doc) {
        await this.doc.destroy().catch(() => {});
        this.doc = null;
      }
      // pdf.js takes ownership of the buffer it is handed, so give it a copy.
      const task = pdfjsLib.getDocument({ data: data.slice(0) });
      this.doc = await task.promise;
      await this.relayout();
      return this.doc.numPages;
    }

    /* ------------------------------------------------------------ layout */

    _fitScale(viewport) {
      const available = Math.max(this.container.clientWidth - 34, 240);
      return available / viewport.width;
    }

    async relayout() {
      if (!this.doc) return;
      const token = ++this.renderToken;

      if (this.observer) this.observer.disconnect();
      this.container.innerHTML = "";
      this.pages.clear();

      const first = await this.doc.getPage(1);
      if (token !== this.renderToken) return;
      this.scale = this._fitScale(first.getViewport({ scale: 1 }));

      for (let number = 1; number <= this.doc.numPages; number += 1) {
        const page = number === 1 ? first : null;
        const viewport = page
          ? page.getViewport({ scale: this.scale })
          : null;

        const wrap = document.createElement("div");
        wrap.className = "pdf-page";
        wrap.dataset.page = String(number);

        const label = document.createElement("span");
        label.className = "pdf-page-num";
        label.textContent = number;

        const canvas = document.createElement("canvas");
        const overlay = document.createElement("div");
        overlay.className = "pdf-overlay";

        wrap.append(canvas, overlay, label);
        this.container.append(wrap);
        this.pages.set(number, { wrap, canvas, overlay, viewport, rendered: false });

        if (viewport) this._size(number, viewport);
        else this._provisionalSize(number, first.getViewport({ scale: this.scale }));
      }

      this.observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              this._render(Number(entry.target.dataset.page), token);
            }
          });
        },
        { root: this.container, rootMargin: "300px 0px" }
      );
      this.pages.forEach((page) => this.observer.observe(page.wrap));
      this.drawHighlights();
    }

    _size(number, viewport) {
      const page = this.pages.get(number);
      if (!page) return;
      page.viewport = viewport;
      const ratio = window.devicePixelRatio || 1;
      page.canvas.width = Math.floor(viewport.width * ratio);
      page.canvas.height = Math.floor(viewport.height * ratio);
      page.canvas.style.width = `${Math.floor(viewport.width)}px`;
      page.canvas.style.height = `${Math.floor(viewport.height)}px`;
      page.wrap.style.width = `${Math.floor(viewport.width)}px`;
      page.wrap.style.height = `${Math.floor(viewport.height)}px`;
    }

    _provisionalSize(number, likeViewport) {
      // Until a page is fetched we assume it matches page one, so the scrollbar
      // is roughly right and nothing jumps when the real size arrives.
      this._size(number, likeViewport);
    }

    async _render(number, token) {
      const page = this.pages.get(number);
      if (!page || page.rendered || !this.doc) return;
      page.rendered = true;
      try {
        const pdfPage = await this.doc.getPage(number);
        if (token !== this.renderToken) return;
        const viewport = pdfPage.getViewport({ scale: this.scale });
        this._size(number, viewport);
        const ratio = window.devicePixelRatio || 1;
        const context = page.canvas.getContext("2d", { alpha: false });
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        await pdfPage.render({ canvasContext: context, viewport }).promise;
        if (token !== this.renderToken) return;
        page.wrap.classList.add("is-rendered");
        this.drawHighlights(number);
      } catch (error) {
        page.rendered = false;
        if (error && error.name !== "RenderingCancelledException") {
          console.warn("page render failed", number, error);
        }
      }
    }

    /* -------------------------------------------------------- highlights */

    setHighlights(highlights) {
      this.highlights = Array.isArray(highlights) ? highlights : [];
      this.drawHighlights();
    }

    setVisible(visible) {
      this.visible = visible;
      this.container.classList.toggle("hide-regions", !visible);
    }

    drawHighlights(only) {
      const byPage = new Map();
      this.highlights.forEach((item) => {
        if (!item || !item.page || !(item.rects || []).length) return;
        if (!byPage.has(item.page)) byPage.set(item.page, []);
        byPage.get(item.page).push(item);
      });

      this.pages.forEach((page, number) => {
        if (only && number !== only) return;
        page.overlay.innerHTML = "";

        // A table row or a block of lines is one layout item, so several fields
        // often resolve to the same rectangle. Drawing one box per field would
        // stack them into a solid slab; they are merged into a single box that
        // names every value it covers.
        const boxes = new Map();
        (byPage.get(number) || []).forEach((item) => {
          (item.rects || []).forEach((rect) => {
            const signature = [rect.x, rect.y, rect.w, rect.h]
              .map((n) => n.toFixed(4))
              .join(":");
            if (!boxes.has(signature)) boxes.set(signature, { rect, items: [] });
            boxes.get(signature).items.push(item);
          });
        });

        boxes.forEach(({ rect, items }) => {
          const box = document.createElement("button");
          box.type = "button";
          box.className = "pdf-hl";
          box.dataset.key = items.map((item) => item.key).join(" ");
          if (items.some((item) => item.key === this.focusKey)) box.classList.add("is-focus");
          if (items.every((item) => (item.issues || []).includes("low_confidence"))) {
            box.classList.add("is-unsure");
          }
          box.style.left = `${rect.x * 100}%`;
          box.style.top = `${rect.y * 100}%`;
          box.style.width = `${rect.w * 100}%`;
          box.style.height = `${rect.h * 100}%`;
          box.title = items
            .map((item) => `${item.label}: ${item.value === null ? "—" : item.value}`)
            .join("\n");
          box.setAttribute("aria-label", box.title.replace(/\n/g, ", "));
          box.addEventListener("click", (event) => {
            event.preventDefault();
            // Repeated clicks walk through the values sharing this region.
            const index = items.findIndex((item) => item.key === this.focusKey);
            this.onSelect(items[(index + 1) % items.length].key);
          });
          page.overlay.append(box);
        });
      });
    }

    focus(key, { scroll = true } = {}) {
      this.focusKey = key;
      this.drawHighlights();
      if (!scroll || !key) return;
      const box = this.container.querySelector(`.pdf-hl[data-key~="${cssEscape(key)}"]`);
      if (!box) return false;
      const wrap = box.closest(".pdf-page");
      const target =
        wrap.offsetTop + box.offsetTop - this.container.clientHeight / 2 + box.offsetHeight / 2;
      this.container.scrollTo({ top: Math.max(target, 0), behavior: "smooth" });
      return true;
    }
  }

  function cssEscape(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/"/g, '\\"');
  }

  function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  window.PdfView = PdfView;
  window.whenPdfReady = whenPdfReady;
})();
