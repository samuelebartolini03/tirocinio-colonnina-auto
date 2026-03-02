import random
import time
import json
import paho.mqtt.client as mqtt  # non usato attivamente qui, ma tenuto per compatibilità
import math
import numpy as np
from scipy.stats import entropy, norm
import matplotlib.pyplot as plt
from collections import defaultdict

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
    "max_potenza": 150,
    "soglia_temp_alta": 55,
    "soglia_temp_critica": 70,
    "soglia_degrado": 90,
    "modalita": "Standard",
    "potenza_massima_stazione": 300,
    "percentuale_riduzione_temp_alta": 0.30,   # ← MODIFICATO (era 0.5)
    "min_power_for_active": 0.5,               # ← MODIFICATO (era 1.0) → ora vede potenza
    "cicli_attesa_blocco": 1,
    "max_raffreddamenti_per_ciclo": 2,
    "prob_fail_raffreddamento_locale": 0.06,
    "prob_fail_raffreddamento_centrale": 0.04,
    "soglia_fail_consecutivi_blocco": 3,
    "aumento_temp_fail_raff": 3.5,
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
                return round(random.uniform(50, 90), 1)
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
        self.beliefs = {
            'p_fail_raff_locale': CONFIG["prob_fail_raffreddamento_locale"],
            'p_fail_raff_centrale': CONFIG["prob_fail_raffreddamento_centrale"],
            'var_temp': 5.0
        }

    def genera_politiche(self):
        return [
            {'raff_locale': True,  'downgrade': False, 'rid_pot': 0.0},
            {'raff_locale': False, 'downgrade': True,  'rid_pot': 0.3},
            {'raff_locale': True,  'downgrade': True,  'rid_pot': 0.1},
            {'raff_locale': False, 'downgrade': False, 'rid_pot': 0.0}
        ]

    def calcola_efe(self, politica, stato_attuale, orizzonte=3):
        efe = 0.0
        for t in range(orizzonte):
            samples = []
            for _ in range(50):
                temp_futura = stato_attuale['temperatura'] + np.random.normal(5, self.beliefs['var_temp'])
                if politica['raff_locale']:
                    if np.random.rand() < self.beliefs['p_fail_raff_locale']:
                        temp_futura += CONFIG['aumento_temp_fail_raff']
                    else:
                        temp_futura -= 10.0
                if politica['downgrade']:
                    temp_futura -= 2.0
                soc_futura = stato_attuale['soc'] + (stato_attuale.get('potenza_effettiva', 0) * (1 - politica['rid_pot']) / 60)
                samples.append({'temp': temp_futura, 'soc': soc_futura})

            temps = [s['temp'] for s in samples]
            mean_temp = np.mean(temps)
            var_temp = np.var(temps)
            mean_soc = np.mean([s['soc'] for s in samples])

            pref_dist_temp = norm(loc=40, scale=5)
            sim_dist_temp = norm(loc=mean_temp, scale=np.sqrt(var_temp))
            kl_prag_temp = self.kl_divergence(sim_dist_temp, pref_dist_temp)
            kl_prag_soc = abs(mean_soc - 100) * 0.1
            kl_prag = kl_prag_temp + kl_prag_soc

            hist, _ = np.histogram(temps, bins=10)
            hist = hist / hist.sum() if hist.sum() > 0 else hist
            ent_epist = entropy(hist)

            efe += kl_prag + ent_epist

        return efe

    def kl_divergence(self, p, q, num_samples=1000):
        samples = p.rvs(num_samples)
        log_ratios = np.log(p.pdf(samples) / q.pdf(samples))
        finite = log_ratios[np.isfinite(log_ratios)]
        return np.mean(finite) if len(finite) > 0 else 0.0

    def aggiorna_beliefs(self, fallito, tipo_raff):
        key = 'p_fail_raff_locale' if tipo_raff == 'locale' else 'p_fail_raff_centrale'
        prior_alpha, prior_beta = 1, 19
        old_p = self.beliefs[key]
        if fallito:
            self.beliefs[key] = (old_p * (prior_alpha + prior_beta) + 1) / (prior_alpha + prior_beta + 1)
        else:
            self.beliefs[key] = (old_p * (prior_alpha + prior_beta)) / (prior_alpha + prior_beta + 1)
        self.beliefs['var_temp'] = max(1.0, self.beliefs['var_temp'] * (1.1 if fallito else 0.9))

    def decide(self, info_globali=None):
        p = self.colonnina.leggi_parametri()
        if info_globali:
            p.update(info_globali)

        temp = p["temperatura"]
        stato_attuale = {
            'temperatura': temp,
            'soc': p['soc'],
            'potenza_effettiva': p.get("potenza_effettiva", 0)
        }

        politiche = self.genera_politiche()
        efe_values = [self.calcola_efe(pol, stato_attuale) for pol in politiche]
        best_idx = np.argmin(efe_values)
        best_pol = politiche[best_idx]
        min_efe = efe_values[best_idx]

        if self.colonnina.modalita == "Eco":
            best_pol['downgrade'] = False

        locale_richiesto = best_pol['raff_locale']
        downgrade_req = best_pol['downgrade']

        voto = 0.35 if temp > CONFIG["soglia_temp_alta"] else 0.0
        if p.get("quante_altre_calda", 0) >= 2:
            voto += 0.25
        voto = min(1.0, voto)

        motiv = f"Temp {temp:.1f}°C | EFE min {min_efe:.2f} | Politica: {best_pol}"
        if locale_richiesto:
            motiv += " → RAFF. LOCALE"

        self.ultima_temp_vista = temp
        self.voto_centrale_ultimo = voto

        return {
            "raffreddamento_locale_richiesto": locale_richiesto,
            "voto_raffreddamento_centrale": round(voto, 2),
            "downgrade_modalita_richiesto": downgrade_req,
            "rid_pot_richiesta": best_pol['rid_pot'],
            "motivazione_agente": motiv,
            "temp": temp,
            "anomalia": p["anomalia"],
            "min_efe": min_efe
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
        self.modalita = "Standard"

        self.s_temp = Sensore("temperatura")
        self.s_temp_ext = Sensore("temperatura_esterna")
        self.s_deg = Sensore("degrado")
        self.s_tens = Sensore("tensione")

        self.raffreddamento_attivo = False
        self.fail_raff_consecutivi = 0
        self.stato_raff_fallito = False
        self.ultimo_raff_usato = None
        self.agente = AgenteLocale(self)

    def reset_raff_fail(self):
        self.fail_raff_consecutivi = 0
        self.stato_raff_fallito = False

    def applica_raffreddamento(self, centrale_attivo: bool, locale_attivo: bool) -> bool:
        raff_attivato = centrale_attivo or locale_attivo
        self.ultimo_raff_usato = "centrale" if centrale_attivo else ("locale" if locale_attivo else None)
        self.raffreddamento_attivo = raff_attivato

        if not raff_attivato:
            self.reset_raff_fail()
            return False

        prob_fail = CONFIG["prob_fail_raffreddamento_locale"]

        fallito = random.random() < prob_fail
        if fallito:
            self.fail_raff_consecutivi += 1
            self.stato_raff_fallito = True

            if self.fail_raff_consecutivi >= CONFIG["soglia_fail_consecutivi_blocco"]:
                self.stato = "BLOCCATA_RAFF_FALLITO"
                self.carica_attiva = False
                print(f"!!! COLONNINA {self.id} → BLOCCATA (raffreddamento fallito {self.fail_raff_consecutivi} volte consecutive)")
                self.agente.aggiorna_beliefs(fallito, self.ultimo_raff_usato)
                return True

            print(f"  Colonnina {self.id}: raffreddamento ({self.ultimo_raff_usato}) FALLITO ({self.fail_raff_consecutivi})")
            self.agente.aggiorna_beliefs(fallito, self.ultimo_raff_usato)
            return False

        self.reset_raff_fail()
        self.agente.aggiorna_beliefs(fallito, self.ultimo_raff_usato)
        return False

    def assegna_auto(self):
        self.veicolo = random.choice(list(VEICOLI.keys()))
        self.capacita = VEICOLI[self.veicolo]["batteria"]
        self.soc_kwh = random.uniform(5, 0.25 * self.capacita)   # ← MODIFICATO (SoC più basso)
        self.carica_attiva = True
        self.stato = "OCCUPATA"
        self.raffreddamento_attivo = False
        self.reset_raff_fail()
        self.modalita = random.choice(["Eco", "Standard", "Boost"])
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
            "modalita": self.modalita,
        }

        if self.stato.startswith("BLOCCATA"):
            dati["alert"] = "COLONNINA BLOCCATA – guasto raffreddamento ripetuto"
        elif self.stato_raff_fallito:
            dati["alert"] = "Raffreddamento fallito questo ciclo"

        return dati

# SERVER
class Server:
    def __init__(self):
        self.quante_colonnine_calide = 0
        self.media_voti_centrali = 0.0

    def distribuisci_potenza(self, lista_parametri):
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
                richieste_raff_locali.append((p["id"], decisione["min_efe"], p))

            if decisione.get("downgrade_modalita_richiesto"):
                richieste_downgrade.append((p["id"], decisione["min_efe"], p))

        richieste_raff_locali.sort(key=lambda x: x[1])

        max_raff_locali_approvabili = CONFIG["max_raffreddamenti_per_ciclo"]
        approvati = 0
        for _, _, p in richieste_raff_locali:
            if approvati >= max_raff_locali_approvabili:
                break
            p["raffreddamento_locale_attivo"] = True
            approvati += 1
            p.setdefault("azioni", []).append(f"RAFF. LOCALE APPROVATO (priorità {approvati})")

        for p in lista_parametri:
            if p.get("stato") != "OCCUPATA":
                continue

            col = next((c for c in colonnine if c.id == p["id"]), None)
            if not col:
                continue

            centrale = False
            locale = p.get("raffreddamento_locale_attivo", False)

            bloccata = col.applica_raffreddamento(centrale, locale)
            if bloccata:
                p["stato"] = col.stato

        richieste_downgrade.sort(key=lambda x: x[1])
        num_downgrade = 0
        for _, _, p in richieste_downgrade:
            if num_downgrade >= 1:
                break
            curr = p.get("modalita_effettiva", p.get("modalita", CONFIG["modalita"]))
            if curr == "Boost":
                p["modalita_effettiva"] = "Eco"
                p.setdefault("azioni", []).append("DOWNGRADE: Boost → Eco")
                num_downgrade += 1

        dati = [dict(p) for p in lista_parametri if p.get("stato") == "OCCUPATA"]
        totale_richiesto = 0.0

        for p in dati:
            req = p.get("potenza_richiesta", 0)
            veicolo = p.get("veicolo")
            modalita = p.get("modalita_effettiva", p.get("modalita", CONFIG["modalita"]))

            if modalita == "Eco":
                req *= 0.75
            elif modalita == "Boost":
                req *= 1.2

            if veicolo:
                req = min(req, VEICOLI[veicolo]["max_potenza"])
            req = min(req, CONFIG["max_potenza"])

            req *= (1 - p["agente"].get("rid_pot_richiesta", 0.0))

            p["richiesta_adjusted"] = round(req, 1)
            totale_richiesto += req

        potenza_disponibile = CONFIG["potenza_massima_stazione"]
        assegnazioni = {p["id"]: 0.0 for p in dati}

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
                
        attive = [p for p in dati if assegnazioni[p["id"]] == 0.0]   # ← MODIFICATO (era != 0.0)

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
    global colonnine
    colonnine = [Colonnina(id=i+1) for i in range(num_colonnine)]
    server = Server()

    stats = {
        'cicli': [],
        'efe_medio': [],
        'temp_medie': [],
        'temp_max': [],
        'soc_medio': [],
        'fallimenti_raff': [],
        'p_fail_medio': [],
        'var_temp_medio': [],
        'potenza_totale': [],
        'anomalie': [],
        'raffinamenti_locali': []
    }

    print(f"\n Avvio simulazione ACTIVE INFERENCE per {40} cicli. {num_colonnine} colonnine.")

    counter_anomalie = 0
    cicli_simulati = 40

    random.seed(42)
    np.random.seed(42)

    for ciclo in range(cicli_simulati):
        print(f"\n --- CICLO {ciclo + 1}/{cicli_simulati} ---")

        parametri_lista = []

        for col in colonnine:
            if col.stato == "LIBERA":
                if random.random() < 0.4:
                    col.assegna_auto()
                else:
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

        server.quante_colonnine_calide = sum(1 for p in parametri_lista if p.get("temperatura", 0) > CONFIG["soglia_temp_alta"] and p.get("stato") == "OCCUPATA")
        voti_centrali = [col.agente.voto_centrale_ultimo for col in colonnine if col.stato == "OCCUPATA"]
        server.media_voti_centrali = sum(voti_centrali) / len(voti_centrali) if voti_centrali else 0.0

        parametri_con_potenza = server.distribuisci_potenza(parametri_lista)

        for p in parametri_con_potenza:
            if p.get("stato") == "OCCUPATA":
                col = next((c for c in colonnine if c.id == p["id"]), None)
                if col:
                    col.aggiorna_soc(p.get("potenza_effettiva", 0))

        # Raccolta statistiche
        efe_values_this_cycle = []
        temps_this_cycle = []
        socs_this_cycle = []
        fallimenti_this_cycle = 0
        p_fails = []
        var_temps = []

        for col in colonnine:
            if col.stato != "OCCUPATA":
                continue
            p = col.leggi_parametri()
            decisione = col.agente.decide({
                "quante_altre_calda": server.quante_colonnine_calide,
                "media_voti_centrali": server.media_voti_centrali
            })
            if "min_efe" in decisione:
                efe_values_this_cycle.append(decisione["min_efe"])
            temps_this_cycle.append(p["temperatura"])
            socs_this_cycle.append(col.soc_percento())
            if col.stato_raff_fallito:
                fallimenti_this_cycle += 1
            p_fails.append(col.agente.beliefs['p_fail_raff_locale'])
            var_temps.append(col.agente.beliefs['var_temp'])

        stats['cicli'].append(ciclo + 1)
        stats['efe_medio'].append(np.mean(efe_values_this_cycle) if efe_values_this_cycle else 0)
        stats['temp_medie'].append(np.mean(temps_this_cycle) if temps_this_cycle else 0)
        stats['temp_max'].append(max(temps_this_cycle) if temps_this_cycle else 0)
        stats['soc_medio'].append(np.mean(socs_this_cycle) if socs_this_cycle else 0)
        stats['fallimenti_raff'].append(fallimenti_this_cycle)
        stats['p_fail_medio'].append(np.mean(p_fails) if p_fails else 0)
        stats['var_temp_medio'].append(np.mean(var_temps) if var_temps else 0)
        stats['anomalie'].append(counter_anomalie)

        totale_kw = sum(p.get("potenza_effettiva", 0) for p in parametri_con_potenza)
        stats['potenza_totale'].append(totale_kw)

        num_raff_locali = sum(1 for p in parametri_con_potenza if p.get("raffreddamento_locale_attivo", False))
        stats['raffinamenti_locali'].append(num_raff_locali)

        for p in parametri_con_potenza:
            if p.get("stato") == "OCCUPATA":
                print(f" Col. {p['id']}: SoC {p['soc']}% | Pot. {p.get('potenza_effettiva',0):.1f} kW | Temp {p.get('temperatura','?')}°C")

        alert, totale_carica = server.analizza_stazione(parametri_con_potenza)
        if alert:
            print(f" ALERT: {alert}")
        print(f" Totale Carica Stazione: {totale_carica} kW")

        time.sleep(0.5)

    # REPORT + GRAFICI
    print("\n" + "="*80)
    print(" RISULTATI SPERIMENTALI – ACTIVE INFERENCE")
    print("="*80)
    print(f"Durata: {cicli_simulati} cicli")
    print(f"Anomalie sensore totali: {counter_anomalie}")
    print(f"Fallimenti raffreddamento totali: {sum(stats['fallimenti_raff'])}")
    print(f"Credenza media finale p(fail raff. locale): {stats['p_fail_medio'][-1]:.3f}")
    print(f"Media potenza erogata: {np.mean(stats['potenza_totale']):.1f} kW")
    print(f"Massima temperatura osservata: {max(stats['temp_max']):.1f} °C")

    fig, axs = plt.subplots(3, 2, figsize=(15, 12))
    fig.suptitle("Risultati sperimentali – Active Inference", fontsize=16)

    axs[0,0].plot(stats['cicli'], stats['efe_medio'], 'b-', label='EFE medio')
    axs[0,0].set_title("Expected Free Energy medio")
    axs[0,0].set_xlabel("Ciclo")
    axs[0,0].set_ylabel("EFE")
    axs[0,0].grid(True)
    axs[0,0].legend()

    axs[0,1].plot(stats['cicli'], stats['temp_medie'], 'r-', label='Temp media')
    axs[0,1].plot(stats['cicli'], stats['temp_max'],   'r--', label='Temp max')
    axs[0,1].axhline(CONFIG["soglia_temp_alta"], color='orange', ls='--', label='Soglia alta')
    axs[0,1].axhline(CONFIG["soglia_temp_critica"], color='darkred', ls='--', label='Soglia critica')
    axs[0,1].set_title("Temperature")
    axs[0,1].set_xlabel("Ciclo")
    axs[0,1].set_ylabel("°C")
    axs[0,1].grid(True)
    axs[0,1].legend()

    axs[1,0].plot(stats['cicli'], stats['soc_medio'], 'g-', label='SoC medio')
    axs[1,0].set_title("Stato di carica medio")
    axs[1,0].set_xlabel("Ciclo")
    axs[1,0].set_ylabel("%")
    axs[1,0].grid(True)
    axs[1,0].legend()

    axs[1,1].bar(stats['cicli'], stats['fallimenti_raff'], color='purple', alpha=0.6)
    axs[1,1].set_title("Fallimenti raffreddamento per ciclo")
    axs[1,1].set_xlabel("Ciclo")
    axs[1,1].set_ylabel("N°")
    axs[1,1].grid(True, axis='y')

    axs[2,0].plot(stats['cicli'], stats['p_fail_medio'], 'm-', label='p(fail raff) medio')
    axs[2,0].plot(stats['cicli'], stats['var_temp_medio'], 'c-', label='var_temp media')
    axs[2,0].set_title("Evoluzione credenze bayesiane")
    axs[2,0].set_xlabel("Ciclo")
    axs[2,0].grid(True)
    axs[2,0].legend()

    axs[2,1].plot(stats['cicli'], stats['potenza_totale'], 'darkgreen', label='Potenza totale')
    axs[2,1].axhline(CONFIG["potenza_massima_stazione"], color='red', ls='--', label='Limite')
    axs[2,1].set_title("Potenza erogata totale")
    axs[2,1].set_xlabel("Ciclo")
    axs[2,1].set_ylabel("kW")
    axs[2,1].grid(True)
    axs[2,1].legend()

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.show()

    print("\nSimulazione completata.\n")
    return stats


if __name__ == "__main__":
    if login():
        avvia_stazione(num_colonnine=4)
    

