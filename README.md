# mychargenet für Home Assistant

Dieses öffentliche Repository stellt die Home-Assistant-App **mychargenet Edge** bereit.
Sie liest ausschließlich die vom Betreiber ausgewählten Leistungssensoren aus Home
Assistant und überträgt sie verschlüsselt an `api.mychargenet.com`.

Die App besitzt keine OCPP-, Schalt- oder Host-Steuerrechte.

## Installation

1. Home Assistant öffnen.
2. **Einstellungen → Apps → App-Store** öffnen.
3. Rechts oben **Repositories** auswählen.
4. Diese Adresse hinzufügen:

   `https://github.com/MarkusTitz/mychargenet-home-assistant`

5. **mychargenet Edge** installieren.
6. Im mychargenet-Portal für den Standort einen Kopplungscode erzeugen.
7. Kopplungscode und die gewünschten Entity-IDs in der App-Konfiguration eintragen.
8. App starten. Nach erfolgreicher Kopplung den Einmalcode wieder aus der
   Konfiguration entfernen.

## Updates

Home Assistant erkennt neue Versionen anhand der App-Version automatisch. Der Kunde
kann ein Update in der App-Ansicht installieren oder automatische Updates aktivieren.

Reine App-Updates sowie zusätzliche Diagnose-, Spannungs- oder Stromsensoren ändern
die sicherheitsrelevante Zählerbindung nicht. Eine neue Inbetriebnahme ist nur nötig,
wenn ein für die Regelung verwendeter Haus- oder Phasen-Leistungssensor geändert wird.

## Unterstützte Systeme

- Home Assistant OS mit Apps/Supervisor
- `amd64`
- `aarch64`, beispielsweise Raspberry Pi 4 oder Raspberry Pi 5

Support: [support@mychargenet.com](mailto:support@mychargenet.com)
