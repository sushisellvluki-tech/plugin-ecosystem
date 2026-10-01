"""Public, data-free shell. All tenant data stays behind authenticated core APIs."""
from pathlib import Path
from starlette.responses import FileResponse
from starlette.routing import Route

ROOT = Path(__file__).parent / 'static'
HEADERS = {
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
}


def routes():
    def asset(name, mime):
        async def endpoint(request):
            return FileResponse(ROOT/name,media_type=mime,headers=HEADERS)
        return endpoint
    return [Route('/cabinet',asset('cabinet.html','text/html')),
            Route('/cabinet/',asset('cabinet.html','text/html')),
            Route('/cabinet/cabinet.js',asset('cabinet.js','text/javascript')),
            Route('/cabinet/cabinet.css',asset('cabinet.css','text/css'))]
