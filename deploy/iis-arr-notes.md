# IIS + ARR in front of the platform

IIS terminates TLS and forwards to Waitress on `127.0.0.1:5000`. The app itself
never listens on a public interface, and port 5000 is never opened in the
security group.

This is step 8 of `docs/DEPLOYMENT.md`; it is expanded here because ARR has
enough moving parts to be worth its own page.

---

## 1. Install the pieces

In **Server Manager → Add Roles and Features**:

- **Web Server (IIS)**, with these role services:
  - Common HTTP Features: Default Document, Static Content, HTTP Errors
  - Health and Diagnostics: HTTP Logging, Request Monitoring
  - Performance: Static Content Compression, Dynamic Content Compression
  - Security: **Request Filtering**
  - Application Development: *(nothing required; the app is pure Python)*
  - Management Tools: IIS Management Console

Then install **Application Request Routing 3.0** and **URL Rewrite 2.1**:

```powershell
# Download from Microsoft first:
#   https://www.iis.net/downloads/microsoft/application-request-routing
#   https://www.iis.net/downloads/microsoft/url-rewrite
# Then:
Start-Process msiexec.exe -Wait -ArgumentList '/i', "$HOME\Downloads\requestRouter_amd64.msi", '/quiet'
Start-Process msiexec.exe -Wait -ArgumentList '/i', "$HOME\Downloads\rewrite_amd64_en-US.msi", '/quiet'
```

## 2. Enable the proxy

ARR is off until you switch it on, and the switch is not in the GUI in a place
anyone finds:

```powershell
Import-Module WebAdministration

# Enable the proxy globally.
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.webServer/proxy' -Name 'enabled' -Value 'True'

# Preserve the Host header the client sent, so Flask sees the real domain
# rather than 127.0.0.1 -- this is what makes redirects and url_for() correct
# behind the proxy. Requires ARR 3.0.
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.webServer/proxy' -Name 'preserveHostHeader' -Value 'True'

# Give the backend a moment before timing out. The client pages poll every five
# seconds and are cheap; this is headroom, not a requirement.
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.webServer/proxy' -Name 'timeout' -Value '00:01:00'
```

## 3. Create the site

```powershell
Import-Module WebAdministration

$siteName = 'AITradingBot'
$root     = 'C:\inetpub\AITradingBot'

New-Item -ItemType Directory -Force $root | Out-Null
New-WebAppPool -Name $siteName -ErrorAction SilentlyContinue
New-Website -Name $siteName -PhysicalPath $root -ApplicationPool $siteName `
            -Port 80 -HostHeader 'trading.example.com'
```

The physical path is only a shell: nothing is served from disk, every request is
proxied. Do not point it at the project directory — `.env` and the database live
there, and an IIS misconfiguration should never be able to serve them.

## 4. The rewrite rule

Put this at `C:\inetpub\AITradingBot\web.config`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>

    <rewrite>
      <rules>
        <!-- Everything goes to Waitress on loopback. -->
        <rule name="ProxyToWaitress" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:5000/{R:1}" />
          <serverVariables>
            <!-- IIS overwrites these by default; the app needs the original. -->
            <set name="HTTP_X_FORWARDED_PROTO"  value="{MY_SCHEME}" />
            <set name="HTTP_X_FORWARDED_HOST"   value="{HTTP_HOST}" />
          </serverVariables>
        </rule>
      </rules>

      <rewriteMaps>
        <rewriteMap name="MY_SCHEME">
          <add key="https" value="https" />
        </rewriteMap>
      </rewriteMaps>
    </rewrite>

    <!-- Allow the two headers the rule sets. Without this the rewrite fails
         at runtime with a 500 and nothing useful in the log. -->
    <security>
      <requestFiltering>
        <allowedServerVariables>
          <add name="HTTP_X_FORWARDED_PROTO" />
          <add name="HTTP_X_FORWARDED_HOST" />
        </allowedServerVariables>
      </requestFiltering>
    </security>

    <!-- The app sets its own headers; do not let IIS add or strip any. -->
    <httpProtocol>
      <customHeaders>
        <remove name="X-Powered-By" />
      </customHeaders>
    </httpProtocol>

    <!-- Static assets are served through the app and are already compressed
         by it; let IIS handle the transport compression. -->
    <urlCompression doStaticCompression="true" doDynamicCompression="true" />

  </system.webServer>
</configuration>
```

Add the allowed server variables through the GUI as well if the XML above is
rejected — **IIS Manager → the site → URL Rewrite → View Server Variables →
Add**.

Simpler alternative if you do not need the forwarded headers: drop the
`<serverVariables>` block entirely and leave `TRUST_PROXY=false` in `.env`. The
app then binds redirects to whatever host it was reached on, which is fine on a
single-domain deployment. `TRUST_PROXY=true` should only be set when a proxy is
genuinely in front and the headers are genuinely arriving — otherwise a client
can forge its own scheme and address.

## 5. HTTPS

```powershell
# win-acme: https://www.win-acme.com/
.\wacs.exe --target iis --host trading.example.com --installation iis
```

It binds 443, creates the certificate, schedules renewal and can add the
HTTP→HTTPS redirect. Add that redirect explicitly if it does not:

```xml
<rule name="ForceHTTPS" stopProcessing="true">
  <match url="(.*)" />
  <conditions>
    <add input="{HTTPS}" pattern="^OFF$" />
  </conditions>
  <action type="Redirect" url="https://{HTTP_HOST}/{R:1}"
          redirectType="Permanent" />
</rule>
```

Put `ForceHTTPS` **above** `ProxyToWaitress`. An unencrypted sign-in form sends
the password in clear text, so this rule is not optional once the site is public.

Only after this rule works should `SESSION_COOKIE_SECURE=true` be set in `.env`.

## 6. Verify

```powershell
# The proxy is reaching the app
curl.exe -sI http://127.0.0.1:5000/health          # direct
curl.exe -sI https://trading.example.com/health    # through IIS

# HTTP redirects rather than serving
curl.exe -sI http://trading.example.com/ | Select-String 'Location|HTTP/'

# Nothing is served from the physical root
curl.exe -s https://trading.example.com/.env | Select-String 'not found|404|HTTP/'
```

That last one matters: it must **not** return the file. The IIS root is a shell
directory for exactly this reason.

Then follow section 10 of `docs/DEPLOYMENT.md` for the full end-to-end checks,
including the client-gets-403-on-/admin test.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 502 / 504 from IIS | The `serve` task is not running, or it is bound to a different port than the rule targets. |
| 500 from IIS, rewrite in the log | `allowedServerVariables` is missing the header the rule sets. |
| Redirects point at `127.0.0.1:5000` | `preserveHostHeader` is off, or `TRUST_PROXY` does not match whether the headers actually arrive. |
| Sign-in works but immediately signs out | `FLASK_SECRET_KEY` is blank, so a temporary key is generated on each restart. Or two web processes are running with different keys. |
| Sign-in form posts but nothing happens | `SESSION_COOKIE_SECURE=true` while the page is being served over HTTP. |
| `.env` or `.db` reachable over the web | The IIS site's physical path was pointed at the project directory. Move it to an empty shell directory. |
