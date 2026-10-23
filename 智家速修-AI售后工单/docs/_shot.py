import asyncio
from playwright.async_api import async_playwright

async def shot(page, html, png, w, h):
    await page.goto(f"file:///{html}")
    await page.wait_for_timeout(600)
    await page.screenshot(path=png, full_page=True)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1080, "height": 1400}, device_scale_factor=2)
        await shot(page,
            "C:/Users/Lenovo/Desktop/f24015431王绍轩2/智家速修-AI售后工单/docs/现状流程图.html",
            "C:/Users/Lenovo/Desktop/f24015431王绍轩2/智家速修-AI售后工单/docs/现状流程图.png", 1080, 1400)
        await shot(page,
            "C:/Users/Lenovo/Desktop/f24015431王绍轩2/智家速修-AI售后工单/docs/数智化转型后流程图.html",
            "C:/Users/Lenovo/Desktop/f24015431王绍轩2/智家速修-AI售后工单/docs/数智化转型后流程图.png", 1080, 1600)
        await browser.close()
        print("OK")

asyncio.run(main())
