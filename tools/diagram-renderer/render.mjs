// Server-only helper. Reads a stored Mermaid diagram on stdin; never calls a model.
import { readFile } from "node:fs/promises";
import { chromium } from "playwright-core";

let input = "";
for await (const chunk of process.stdin) {
  input += chunk;
  if (input.length > 12000) process.exit(2);
}
let browser;
let server;
let watchdog;
let timedOut = false;
let initialized = false;
try {
  const { code, budget_ms = 17000 } = JSON.parse(input);
  if (typeof code !== "string" || code.length > 5000 || !/^\s*(flowchart|graph)\b/.test(code)) {
    process.exitCode = 2;
  } else {
    const script = await readFile(new URL("./node_modules/mermaid/dist/mermaid.min.js", import.meta.url), "utf8");
    const began = Date.now();
    const budget = Math.max(2000, Math.min(57000, Number(budget_ms)));
    server = await chromium.launchServer({
      headless: true,
      chromiumSandbox: process.env.DIAGRAM_BROWSER_SANDBOX !== "0",
      timeout: Math.min(10000, budget),
      ...(process.env.DIAGRAM_BROWSER_EXECUTABLE ? { executablePath: process.env.DIAGRAM_BROWSER_EXECUTABLE } : {}),
    });
    watchdog = setTimeout(() => { timedOut = true; void server.kill(); }, Math.max(100, budget - (Date.now() - began)));
    browser = await chromium.connect(server.wsEndpoint());
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, colorScheme: "light", offline: true });
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    await page.setContent('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0"><div id="root" style="width:1200px"></div></body></html>');
    await page.addScriptTag({ content: script });
    initialized = true;
    const result = await page.evaluate(async (code) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default", maxTextSize: 5000, maxEdges: 200, suppressErrorRendering: true });
      const { svg } = await mermaid.render("qn_saved_diagram", code);
      if (svg.length > 1500000) throw new Error("Unsupported output size");
      const root = document.getElementById("root");
      root.innerHTML = svg;
      await document.fonts.ready;
      const element = root.querySelector("svg");
      if (!element) throw new Error("No diagram");
      const targets = [...element.querySelectorAll("foreignObject,text")].filter((node) =>
        !node.closest("defs,marker,clipPath,mask") && !node.parentElement.closest("foreignObject,text")
      );
      const labels = targets.map((node) => {
        const text = (node.querySelector("div")?.innerText || node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
        if (!text) return null;
        const b = node.getBBox();
        const toRoot = element.getScreenCTM().inverse().multiply(node.getScreenCTM());
        const points = [[b.x,b.y],[b.x+b.width,b.y],[b.x,b.y+b.height],[b.x+b.width,b.y+b.height]].map(([x,y])=>new DOMPoint(x,y).matrixTransform(toRoot));
        const x = Math.min(...points.map(p=>p.x)), y = Math.min(...points.map(p=>p.y));
        const width = Math.max(...points.map(p=>p.x))-x, height = Math.max(...points.map(p=>p.y))-y;
        const path=[];
        for(let n=node;n!==element;n=n.parentElement) path.unshift([...n.parentElement.children].indexOf(n));
        return {text,x,y,width,height,path};
      }).filter(Boolean);
      if (labels.length < 2 || labels.length > 40 || labels.some(l=>!l.width || !l.height || l.text.length>300)) throw new Error("Unsupported labels");
      return { svg: new XMLSerializer().serializeToString(element), labels, view_box: element.getAttribute("viewBox") };
    }, code);
    result.renderer = { mermaid: "12.2.0", browser: browser.version(), theme: "default", viewport_width: 1200 };
    process.stdout.write(JSON.stringify(result));
  }
} catch {
  process.stderr.write("Diagram snapshot rendering failed.\n");
  process.exitCode = initialized && !timedOut ? 2 : 1;
} finally {
  clearTimeout(watchdog);
  if (server) await server.kill();
}
