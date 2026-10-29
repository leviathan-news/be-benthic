() => {
    const visibility = new WeakMap();
    function visible(element) {
        if (!element) return true;
        if (visibility.has(element)) return visibility.get(element);
        const style = getComputedStyle(element);
        const shown = !element.matches('[hidden], [inert], [aria-hidden="true"], dialog:not([open])')
            && style.display !== 'none' && style.visibility !== 'hidden'
            && style.visibility !== 'collapse' && Number(style.opacity) !== 0
            && style.fontSize !== '0px' && visible(element.parentElement);
        visibility.set(element, shown);
        return shown;
    }
    const roots = Array.from(document.querySelectorAll('article, main'));
    const root = roots.length ? roots.find(el => visible(el) && el.getClientRects().length) : document.body;
    if (!root || !visible(root)) return {usable: false};
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const parts = [];
    let node, count = 0, chars = 0, bounded = false;
    while ((node = walker.nextNode())) {
        if (++count > 10000 || chars > 48000) { bounded = true; break; }
        const parent = node.parentElement;
        if (!parent || !visible(parent) || parent.closest('script, style, template, noscript, svg, head, title, nav, footer, aside')) continue;
        if (root === document.body && parent.closest('header')) continue;
        const details = parent.closest('details:not([open])');
        if (details && !parent.closest('summary')) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        if (!Array.from(range.getClientRects()).some(rect => rect.width > 0 && rect.height > 0)) continue;
        const text = node.textContent.replace(/\s+/g, ' ').trim();
        if (text) { parts.push(text); chars += text.length; }
    }
    const text = parts.join('\n');
    const shell = text.length < 1500 && /^(just a moment|checking your browser|verify (?:that )?you are human|access denied|enable javascript|sign in to continue|loading[.\s])/i.test(text);
    const password = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
    const usable = text.length >= 160 && !shell && !password;
    const bytes = new TextEncoder().encode(text);
    let end = Math.min(bytes.length, 8000);
    while (end < bytes.length && end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
    return {usable, type: 'result', text: new TextDecoder().decode(bytes.slice(0, end)),
            url: location.href, truncated: bounded || end < bytes.length};
}
