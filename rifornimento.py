import random
import time
import json
import paho.mqtt.client as mqtt
import math

# SISTEMA DI AUTENTICAZIONE
UTENTI = {
    "admin": "1234",
    "tecnico": "password",
    "ospite": "guest"
}

def login():
    print(" ACCESSO SICURO ALLA STAZIONE DI RICARICA")
    tentativi = 3
    while tentativi > 0:
        user = input("Username: ").strip()
        pwd = input("Password: ").strip()
        if user in UTENTI and UTENTI[user] == pwd:
            print("\n Accesso consentito. Benvenuto,", user, "\n")
            return True
        tentativi -= 1
        print(f" Credenziali errate. Tentativi rimasti: {tentativi}")
    print(" Troppi tentativi falliti. Uscita dal sistema.\n")
    exit()

# CONFIGURAZIONE STAZIONE
CONFIG = {
    "max_potenza": 150,                        # max potenza per colonnina (kW)
    "soglia_temp_alta": 55,                    # in °C (inizia gestione raffreddamento)
    "soglia_temp_critica": 70,                 # in °C (sospendi carica)
    "soglia_degrado": 90,                      # soglia degrado (sospendi carica)
    "modalita": "Standard",                    # Standard, Eco, Boost
    "potenza_massima_stazione": 300,           # kW totale
    "percentuale_riduzione_temp_alta": 0.5,    # riduzione potenza su temp alta (50%)
    "min_power_for_active": 1.0, # minima kW per colonnina considerata "attiva"
    "cicli_attesa_blocco": 1,
    "max_raffreddamenti_per_ciclo": 2,
    "prob_fail_raffreddamento_locale": 0.06,      # 6% probabilità fallimento locale
    "prob_fail_raffreddamento_centrale": 0.04,    # 4% probabilità fallimento centrale
    "soglia_fail_consecutivi_blocco": 3,          # dopo quanti fallimenti → blocco
    "aumento_temp_fail_raff": 3.5,                # °C extra se fallisce
}

VEICOLI = {
    "CityCar": {"batteria": 40, "max_potenza": 50},
    "SUV": {"batteria": 80, "max_potenza": 120},
    "Sportiva": {"batteria": 100, "max_potenza": 150}
}
# SENSORI
class Sensore:
    def __init__(self, tipo):
        self.tipo = tipo

    def rileva(self):
        if self.tipo == "temperatura":
            if random.random() < 0.1:
                return round(random.uniform(50, 90), 1)  # Spike
            return round(random.uniform(20, 40), 1)
        elif self.tipo == "temperatura_esterna":
            return round(random.uniform(10, 45), 1)
        elif self.tipo == "degrado":
            return round(random.uniform(5, 15), 1)
        elif self.tipo == "tensione":
            return round(random.uniform(350, 800), 1)
        return 0.0

# AGENTE LOCALE
class AgenteLocale:
    def __init__(self, colonnina):
        self.colonnina = colonnina
        self.isteresi_raff_locale = 3.0
        self.ultima_temp_vista = 25.0
        self.voto_centrale_ultimo = 0.0

    def decide(self, info_globali=None):
        p = self.colonnina.leggi_parametri()
        if info_globali:
            p.update(info_globali)

        temp = p["temperatura"]
        potenza = p.get("potenza_effettiva", 0)

        # Raffreddamento locale
        soglia = CONFIG["soglia_temp_alta"]
        if self.ultima_temp_vista > CONFIG["soglia_temp_alta"] + 2:
            soglia -= self.isteresi_raff_locale

        locale_richiesto = temp >= soglia and potenza > CONFIG["min_power_for_active"] * 1.5

        # Voto per centrale
        voto = 0.0
        if temp >= CONFIG["soglia_temp_critica"]:
            voto = 1.0
        elif temp > CONFIG["soglia_temp_alta"] + 8:
            voto = 0.85
        elif temp > CONFIG["soglia_temp_alta"] + 3:
            voto = 0.60
        elif temp > CONFIG["soglia_temp_alta"]:
            voto = 0.35

        if p.get("quante_altre_calda", 0) >= 2:
            voto = min(1.0, voto + 0.25)

        # Downgrade modalità
        downgrade_req = temp >= CONFIG["soglia_temp_alta"] + 5

        motiv = f"Temp {temp:.1f}°C | Voto centrale {voto:.2f}"
        if locale_richiesto:
            motiv += " → RAFF. LOCALE RICHIESTO"

        self.ultima_temp_vista = temp
        self.voto_centrale_ultimo = voto

        return {
            "raffreddamento_locale_richiesto": locale_richiesto,
            "voto_raffreddamento_centrale": round(voto, 2),
            "downgrade_modalita_richiesto": downgrade_req,
            "motivazione_agente": motiv,
            "temp": temp,
            "anomalia": p["anomalia"]
        }

# COLONNINA
class Colonnina:
    def __init__(self, id):
        self.id = id
        self.veicolo = None
        self.capacita = None
        self.soc_kwh = 0.0
        self.carica_attiva = False
        self.stato = "LIBERA"

        self.s_temp = Sensore("temperatura")
        self.s_temp_ext = Sensore("temperatura_esterna")
        self.s_deg = Sensore("degrado")
        self.s_tens = Sensore("tensione")

        self.raffreddamento_attivo = False           # applicato questo ciclo (locale)

        # Gestione guasti raffreddamento
        self.fail_raff_consecutivi = 0
        self.stato_raff_fallito = False
        self.ultimo_raff_usato = None
        self.agente = AgenteLocale(self)

    def reset_raff_fail(self):
        self.fail_raff_consecutivi = 0
        self.stato_raff_fallito = False

    def applica_raffreddamento(self, centrale_attivo: bool, locale_attivo: bool) -> bool:
        """ Applica raffreddamento e restituisce True se la colonnina è stata BLOCCATA """
        raff_attivato = centrale_attivo or locale_attivo
        self.ultimo_raff_usato = "centrale" if centrale_attivo else ("locale" if locale_attivo else None)
        self.raffreddamento_attivo = raff_attivato

        if not raff_attivato:
            self.reset_raff_fail()
            return False

        # Probabilità di fallimento
        prob_fail = (
            CONFIG["prob_fail_raffreddamento_centrale"]
            if centrale_attivo else
            CONFIG["prob_fail_raffreddamento_locale"]
        )

        if random.random() < prob_fail:
            self.fail_raff_consecutivi += 1
            self.stato_raff_fallito = True

            if self.fail_raff_consecutivi >= CONFIG["soglia_fail_consecutivi_blocco"]:
                self.stato = "BLOCCATA_RAFF_FALLITO"
                self.carica_attiva = False
                print(f"!!! COLONNINA {self.id} → BLOCCATA "
                      f"(raffreddamento fallito {self.fail_raff_consecutivi} volte consecutive)")
                return True

            print(f"  Colonnina {self.id}: raffreddamento ({self.ultimo_raff_usato}) FALLITO "
                  f"({self.fail_raff_consecutivi})")
            return False

        # successo
        self.reset_raff_fail()
        return False

    def assegna_auto(self):
        self.veicolo = random.choice(list(VEICOLI.keys()))
        self.capacita = VEICOLI[self.veicolo]["batteria"]
        self.soc_kwh = random.uniform(5, 0.3 * self.capacita)
        self.carica_attiva = True
        self.stato = "OCCUPATA"
        self.raffreddamento_attivo = False
        self.reset_raff_fail()
        print(f" Nuova auto ({self.veicolo}) sulla colonnina {self.id}")

    def aggiorna_soc(self, potenza_effettiva: float):
        if not self.carica_attiva or self.stato.startswith("BLOCCATA"):
            return
        self.soc_kwh = min(self.capacita, self.soc_kwh + (potenza_effettiva / 60.0))
        if self.soc_kwh >= self.capacita * 0.98:
            self.stato = "COMPLETATA"
            self.carica_attiva = False
            print(f" Colonnina {self.id}: ricarica completata. Auto in partenza.")

    def soc_percento(self) -> float:
        if not self.veicolo or self.capacita is None:
            return 0.0
        return round((self.soc_kwh / self.capacita) * 100, 1)

    def leggi_parametri(self) -> dict:
        t_esterna = self.s_temp_ext.rileva()
        t_reale_sensore = self.s_temp.rileva()

        if self.stato.startswith("BLOCCATA"):
            potenza_teorica = 0.0
            temp_predetta = t_esterna + 1.0
            inc_teorico = 0
        else:
            if self.stato == "OCCUPATA":
                max_p = VEICOLI[self.veicolo]["max_potenza"]
                potenza_teorica = random.uniform(max_p * 0.5, max_p)
                inc_teorico = (potenza_teorica / 10.0) * 2.0
                if self.raffreddamento_attivo:
                    inc_teorico *= 0.6
                temp_predetta = t_esterna + inc_teorico
            else:
                potenza_teorica = 0
                temp_predetta = t_esterna + 1.0
                inc_teorico = 0

        gap = abs(t_reale_sensore - temp_predetta)
        soglia_anomalia = 15.0
        soglia_media_pericolosa = 45.0
        media_temp = (t_reale_sensore + temp_predetta) / 2

        anomalia = gap > soglia_anomalia
        anomalia_pericolosa = anomalia and (media_temp > soglia_media_pericolosa)

        diagnostica = "OK"
        if anomalia:
            diagnostica = f"ANOMALIA SENSORE: gap {gap:.1f}°C"
            if anomalia_pericolosa:
                diagnostica += f" – MEDIA ALTA {media_temp:.1f}°C → ATTENZIONE!"

        if self.stato_raff_fallito and self.raffreddamento_attivo:
            diagnostica += " – RAFF. FALLITO QUESTO CICLO"

        temperatura_output = max(t_reale_sensore, temp_predetta) if anomalia else t_reale_sensore

        if self.stato.startswith("BLOCCATA"):
            temperatura_output = max(temperatura_output, 75.0)
            diagnostica += " – COLONNINA BLOCCATA (guasto raffreddamento)"

        dati = {
            "id": self.id,
            "veicolo": self.veicolo if self.stato == "OCCUPATA" else None,
            "stato": self.stato,
            "soc": self.soc_percento(),
            "temperatura": round(temperatura_output, 1),
            "temperatura_esterna": t_esterna,
            "temperatura_predetta": round(temp_predetta, 1),
            "temperatura_reale_grezzo": round(t_reale_sensore, 1),
            "gap_rilevato": round(gap, 1),
            "anomalia": anomalia,
            "anomalia_pericolosa": anomalia_pericolosa,
            "media_temperatura": round(media_temp, 1),
            "diagnostica": diagnostica,
            "degrado": self.s_deg.rileva(),
            "tensione": self.s_tens.rileva(),
            "potenza_richiesta": round(potenza_teorica, 1),
            "raffreddamento_attivo": self.raffreddamento_attivo,
            "fail_raff_consecutivi": self.fail_raff_consecutivi,
        }

        if self.stato.startswith("BLOCCATA"):
            dati["alert"] = "COLONNINA BLOCCATA – guasto raffreddamento ripetuto"
        elif self.stato_raff_fallito:
            dati["alert"] = "Raffreddamento fallito questo ciclo"

        return dati

# SERVER
class Server:
    def __init__(self):
        self.raffreddamento_centrale_attivo = False
        self.quante_colonnine_calide = 0
        self.media_voti_centrali = 0.0

    def distribuisci_potenza(self, lista_parametri):
        # Fase 1: decisioni agenti e richieste
        richieste_raff_locali = []
        richieste_downgrade = []

        for p in lista_parametri:
            if p.get("stato") != "OCCUPATA":
                continue

            col = next((c for c in colonnine if c.id == p["id"]), None) 
            if not col:
                continue

            decisione = col.agente.decide({
                "quante_altre_calda": self.quante_colonnine_calide,
                "media_voti_centrali": self.media_voti_centrali
            })
            p["agente"] = decisione

            if decisione.get("raffreddamento_locale_richiesto"):
                richieste_raff_locali.append((p["id"], decisione["temp"], p))

            if decisione.get("downgrade_modalita_richiesto"):
                richieste_downgrade.append((p["id"], decisione["temp"], p))

        # Fase 2: max 2 raffreddamenti
        attivati_raff = 0
        richieste_raff_locali.sort(key=lambda x: x[1], reverse=True)

        if self.media_voti_centrali >= 0.70 or self.quante_colonnine_calide >= 3:
            if attivati_raff < CONFIG["max_raffreddamenti_per_ciclo"]:
                self.raffreddamento_centrale_attivo = True
                attivati_raff += 1

        for idc, _, p in richieste_raff_locali:
            if attivati_raff >= CONFIG["max_raffreddamenti_per_ciclo"]:
                break
            if not self.raffreddamento_centrale_attivo:
                p["raffreddamento_locale_attivo"] = True
                attivati_raff += 1

        # Fase 3: applica raffreddamento con rischio fallimento
        for p in lista_parametri:
            if p.get("stato") != "OCCUPATA":
                continue

            col = next((c for c in colonnine if c.id == p["id"]), None)
            if not col:
                continue

            centrale = self.raffreddamento_centrale_attivo
            locale = p.get("raffreddamento_locale_attivo", False)

            bloccata = col.applica_raffreddamento(centrale, locale)
            if bloccata:
                p["stato"] = col.stato  # Aggiorna p con nuovo stato

        # Fase 4: downgrade modalità locale
        for _, _, p in richieste_downgrade:
            curr = p.get("modalita_effettiva", CONFIG["modalita"])
            if curr == "Boost":
                p["modalita_effettiva"] = "Standard"
                p.setdefault("azioni", []).append("DOWNGRADE: Boost → Standard")
            elif curr == "Standard":
                p["modalita_effettiva"] = "Eco"
                p.setdefault("azioni", []).append("DOWNGRADE: Standard → Eco")

        # Fase 5: riduzione extra se centrale
        if self.raffreddamento_centrale_attivo:
            for p in lista_parametri:
                if p.get("potenza_effettiva", 0) > 0 and p.get("stato") == "OCCUPATA":
                    if "potenza_effettiva" in p:
                        p["potenza_effettiva"] *= 0.80
                        p["potenza_effettiva"] = round(p["potenza_effettiva"], 1)
                    p.setdefault("azioni", []).append("RIDUZ. GLOBALE: raffr. centrale")

        # Distribuzione potenza
        dati = [dict(p) for p in lista_parametri if p.get("stato") == "OCCUPATA"]
        totale_richiesto = 0.0

        for p in dati:
            req = p.get("potenza_richiesta", 0)
            veicolo = p.get("veicolo")
            modalita = p.get("modalita_effettiva", CONFIG["modalita"])

            if modalita == "Eco":
                req *= 0.75
            elif modalita == "Boost":
                req *= 1.2

            if veicolo:
                req = min(req, VEICOLI[veicolo]["max_potenza"])
            req = min(req, CONFIG["max_potenza"])

            p["richiesta_adjusted"] = round(req, 1)
            totale_richiesto += req

        potenza_disponibile = CONFIG["potenza_massima_stazione"]
        assegnazioni = {p["id"]: 0.0 for p in dati}

        # Sospensioni immediate per casi critici
        for p in dati:
            idc = p["id"]
            if p["stato"] == "BLOCCATA_RAFF_FALLITO":
                assegnazioni[idc] = 0.0
                p.setdefault("azioni", []).append("FERMA: BLOCCATA RAFF FALLITO")
            elif p["temperatura"] > CONFIG["soglia_temp_critica"]:
                assegnazioni[idc] = 0.0
                p.setdefault("azioni", []).append("FERMA: Temp Critica")
            elif p.get("degrado", 0) > CONFIG["soglia_degrado"]:
                assegnazioni[idc] = 0.0
                p.setdefault("azioni", []).append("FERMA: Degrado Alto")

        attive = [p for p in dati if assegnazioni[p["id"]] != 0.0]

        if not attive:
            out = []
            for p in lista_parametri:
                if p.get("stato") != "OCCUPATA":
                    p["azioni"], p["potenza_effettiva"] = ["LIBERA"], 0
                else:
                    p["azioni"], p["potenza_effettiva"] = ["FERMA: Critico o BLOCCATA"], 0
                out.append(p)
            return out

        attive_sorted = sorted(attive, key=lambda x: x.get("soc", 100.0))

        totale_attive_richieste = sum(p["richiesta_adjusted"] for p in attive_sorted)

        if totale_attive_richieste <= potenza_disponibile:
            for p in attive_sorted:
                idc = p["id"]
                alloc = p["richiesta_adjusted"]
                if p["temperatura"] > CONFIG["soglia_temp_alta"] and p["temperatura"] <= CONFIG["soglia_temp_critica"]:
                    alloc *= (1 - CONFIG["percentuale_riduzione_temp_alta"])
                    p.setdefault("azioni", []).append(f"RIDUCI: Temp Alta (-{int(CONFIG['percentuale_riduzione_temp_alta']*100)}%)")
                else:
                    p.setdefault("azioni", []).append("OK")
                assegnazioni[idc] = round(max(0, alloc), 1)
        else:
            restante = potenza_disponibile
            for p in attive_sorted:
                idc = p["id"]
                richi = p["richiesta_adjusted"]
                riduzione = 1.0
                if p["temperatura"] > CONFIG["soglia_temp_alta"] and p["temperatura"] <= CONFIG["soglia_temp_critica"]:
                    riduzione = (1 - CONFIG["percentuale_riduzione_temp_alta"])
                    p.setdefault("azioni", []).append(f"RIDUCI: Temp Alta (-{int(CONFIG['percentuale_riduzione_temp_alta']*100)}%)")

                richi_mod = richi * riduzione
                asseg = min(richi_mod, restante)

                if restante <= 0 or asseg < CONFIG["min_power_for_active"]:
                    assegnazioni[idc] = 0.0
                    p.setdefault("azioni", []).append("RIPOSO: Potenza Non Disponibile")
                else:
                    assegnazioni[idc] = round(max(0.0, asseg), 1)
                    p.setdefault("azioni", []).append("OK (Priorità SOC bassa)")
                    restante -= assegnazioni[idc]

            if restante > 0:
                for p in attive_sorted:
                    idc = p["id"]
                    richi = p["richiesta_adjusted"]
                    current = assegnazioni[idc]
                    max_add = max(0.0, richi - current)
                    if max_add <= 0:
                        continue
                    add = min(max_add, restante)
                    assegnazioni[idc] = round(current + add, 1)
                    restante -= add
                    if restante <= 0:
                        break

        # Costruzione output finale
        out = []
        for p in lista_parametri:
            if p.get("stato") != "OCCUPATA":
                p["azioni"], p["potenza_effettiva"] = ["LIBERA"], 0
            else:
                pid = p["id"]
                pot_eff = assegnazioni.get(pid, 0.0)
                pot_eff = round(max(0.0, min(pot_eff, CONFIG["max_potenza"])), 1)
                p["potenza_effettiva"] = pot_eff
                if "azioni" not in p or not p["azioni"]:
                    p["azioni"] = ["OK"]
            out.append(p)

        return out

    def analizza_stazione(self, parametri_con_potenza):
        totale_kw = sum(p.get("potenza_effettiva", 0) for p in parametri_con_potenza)
        alert = None
        num_critiche = sum(1 for p in parametri_con_potenza if p.get("temperatura", 0) > CONFIG["soglia_temp_critica"])
        num_bloccate = sum(1 for p in parametri_con_potenza if p.get("stato", "").startswith("BLOCCATA"))
        if num_critiche > 0:
            alert = f"ATTENZIONE: {num_critiche} colonnine con temperatura critica!"
        elif num_bloccate > 0:
            alert = f"ATTENZIONE: {num_bloccate} colonnine bloccate per guasto raffreddamento!"
        return alert, round(totale_kw, 1)

def avvia_stazione(num_colonnine=4):
    global colonnine  # Temporaneo, in produzione usa dependency injection
    colonnine = [Colonnina(id=i+1) for i in range(num_colonnine)]
    server = Server()

    counter_anomalie = 0
    cicli_simulati = 10
    print(f"\n Avvio simulazione per {cicli_simulati} cicli. {num_colonnine} colonnine.")

    for ciclo in range(cicli_simulati):
        print(f"\n --- CICLO {ciclo + 1}/{cicli_simulati} ---")

        parametri_lista = []

        for col in colonnine:
            if col.stato == "LIBERA":
                if random.random() < 0.4:
                    col.assegna_auto()
                else:
                    print(f" Colonnina {col.id} è LIBERA e in attesa.")
                    parametri_lista.append(col.leggi_parametri())
                    continue

            if col.stato == "COMPLETATA":
                print(f" Colonnina {col.id} è stata liberata.")
                col.stato = "LIBERA"
                col.veicolo = None
                col.raffreddamento_attivo = False
                col.reset_raff_fail()
                parametri_lista.append(col.leggi_parametri())
                continue

            p = col.leggi_parametri()
            parametri_lista.append(p)

            if p.get("anomalia"):
                counter_anomalie += 1

        # Calcola globali per server
        server.quante_colonnine_calide = sum(1 for p in parametri_lista if p.get("temperatura", 0) > CONFIG["soglia_temp_alta"] and p.get("stato") == "OCCUPATA")
        voti_centrali = [col.agente.voto_centrale_ultimo for col in colonnine if col.stato == "OCCUPATA"]
        server.media_voti_centrali = sum(voti_centrali) / len(voti_centrali) if voti_centrali else 0.0

        parametri_con_potenza = server.distribuisci_potenza(parametri_lista)

        for p in parametri_con_potenza:
            if p.get("stato") != "OCCUPATA":
                continue

            col = next((c for c in colonnine if c.id == p["id"]), None)
            if not col:
                continue

            potenza_effettiva = p.get("potenza_effettiva", 0.0)
            col.raffreddamento_attivo = p.get("raffreddamento_locale_attivo", False) or server.raffreddamento_centrale_attivo
            col.aggiorna_soc(potenza_effettiva)

            payload = {
                "id": col.id,
                "veicolo": col.veicolo,
                "stato": col.stato,
                "soc": col.soc_percento(),
                "temperatura": p.get("temperatura"),
                "temperatura_esterna": p.get("temperatura_esterna"),
                "temperatura_predetta": p.get("temperatura_predetta"),
                "gap_rilevato": p.get("gap_rilevato"),
                "anomalia": p.get("anomalia"),
                "anomalia_pericolosa": p.get("anomalia_pericolosa"),
                "media_temperatura": p.get("media_temperatura"),
                "diagnostica": p.get("diagnostica"),
                "degrado": p.get("degrado"),
                "tensione": p.get("tensione"),
                "potenza_richiesta": p.get("potenza_richiesta"),
                "potenza_effettiva": potenza_effettiva,
                "azioni": p.get("azioni", []),
                "raffreddamento_attivo": col.raffreddamento_attivo,
                "fail_raff_consecutivi": col.fail_raff_consecutivi,
                "modalita_effettiva": p.get("modalita_effettiva", CONFIG["modalita"]),
            }

            if "agente" in p:
                payload.update({
                    "voto_raff_centrale": p["agente"].get("voto_raffreddamento_centrale"),
                    "motivazione_agente": p["agente"].get("motivazione_agente"),
                })

            status_icon = "⚠️" if payload.get("anomalia") else "✅"
            alert_text = payload.get("alert", "")
            veicolo_str = payload.get("veicolo", "—")
            print(f"   Col. {col.id} ({veicolo_str}): SoC {payload['soc']}% | "
                  f"Pot. Eff. {potenza_effettiva:.1f} kW | "
                  f"Temp: {payload.get('temperatura', '?')}°C")
            print(f"      {status_icon} {payload.get('diagnostica', 'OK')} {alert_text}")

        alert, totale_carica = server.analizza_stazione(parametri_con_potenza)

        server_data = {
            "timestamp": time.time(),
            "totale_carica_kw": totale_carica,
            "alert_stazione": alert,
            "modalita_stazione": CONFIG["modalita"],
            "raffreddamento_centrale_attivo": server.raffreddamento_centrale_attivo,
        }

        # client.publish(MQTT_TOPIC_TELEMETRY, json.dumps({"colonnine": parametri_con_potenza}))  # Uncomment
        # client.publish(MQTT_TOPIC_SERVER, json.dumps(server_data))  # Uncomment

        if alert:
            print(f" ALERT STAZIONE: {alert}")
        if server.raffreddamento_centrale_attivo:
            print(" Sistema di raffreddamento centrale ATTIVO (temperature alte rilevate).")
        print(f" Totale Carica Stazione: {totale_carica} kW")

        time.sleep(2)

    print("\n" + "=" * 50)
    print(" SIMULAZIONE COMPLETATA")
    print(f" REPORT DIAGNOSTICA: Riscontrate {counter_anomalie} anomalie dei sensori.")
    print("=" * 50)

if __name__ == "__main__":
    if login():
        avvia_stazione(num_colonnine=4)
