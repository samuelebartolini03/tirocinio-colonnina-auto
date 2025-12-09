import random
import time
import json
import paho.mqtt.client as mqtt

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
    "modalita": "Standard",  # Può essere Standard, Eco, o Boost
    "potenza_massima_stazione": 300
}

VEICOLI = {
    "CityCar": {"batteria": 40, "max_potenza": 50},
    "SUV": {"batteria": 80, "max_potenza": 120},
    "Sportiva": {"batteria": 100, "max_potenza": 150}
}

# MQTT
MQTT_BROKER = "localhost"  # Modificare se il broker non è locale
MQTT_PORT = 1883
MQTT_TOPIC_TELEMETRY = "ev/stazione"
MQTT_TOPIC_SERVER = "ev/stazione/server"

client = mqtt.Client()
try:
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    print(f" Connesso al broker MQTT su {MQTT_BROKER}:{MQTT_PORT}")
except Exception as e:
    print(f" ERRORE: Impossibile connettersi al broker MQTT. {e}")
    pass


# SENSORI
class Sensore:
    def __init__(self, tipo):
        self.tipo = tipo

    def rileva(self):
        if self.tipo == "temperatura":
            # Più probabile che la temperatura sia normale (20-40)
            if random.random() < 0.1:
                return round(random.uniform(50, 90), 1)  # Spike
            return round(random.uniform(20, 40), 1)
        elif self.tipo == "temperatura_esterna":
            return round(random.uniform(10, 45), 1)
        elif self.tipo == "degrado":
            return round(random.uniform(5, 15), 1)
        elif self.tipo == "tensione":
            return round(random.uniform(350, 800), 1)


# MODELLO COLONNINA
class Colonnina:
    def __init__(self, id):
        self.id = id
        self.veicolo = None
        self.capacita = None
        self.soc_kwh = 0
        self.carica_attiva = False
        self.stato = "LIBERA"

        self.s_temp = Sensore("temperatura")
        self.s_temp_ext = Sensore("temperatura_esterna")
        self.s_deg = Sensore("degrado")
        self.s_tens = Sensore("tensione")

    def assegna_auto(self):
        self.veicolo = random.choice(list(VEICOLI.keys()))
        self.capacita = VEICOLI[self.veicolo]["batteria"]
        # Inizia con una carica bassa
        self.soc_kwh = random.uniform(5, 0.3 * self.capacita)
        self.carica_attiva = True
        self.stato = "OCCUPATA"
        print(f" Nuova auto ({self.veicolo}) sulla colonnina {self.id}")

    def aggiorna_soc(self, potenza_effettiva):
        if self.carica_attiva:
            # Aggiorna SoC in base alla potenza effettivamente erogata
            self.soc_kwh = min(self.capacita, self.soc_kwh + (potenza_effettiva / 60))

            if self.soc_kwh >= self.capacita * 0.98:  # Considera carica completata al 98%
                self.stato = "COMPLETATA"
                self.carica_attiva = False
                print(f" Colonnina {self.id}: ricarica completata. Auto in partenza.")

    def soc_percento(self):
        if not self.veicolo or self.capacita is None:
            return 0
        return round((self.soc_kwh / self.capacita) * 100, 1)

    def leggi_parametri(self):
        if self.stato != "OCCUPATA":
            return {
                "id": self.id,
                "stato": "LIBERA",
                "veicolo": None,
                "soc": 0,
                "temperatura": self.s_temp.rileva(),  # Sensori attivi anche se non carica
                "temperatura_esterna": self.s_temp_ext.rileva(),
                "degrado": self.s_deg.rileva(),
                "tensione": self.s_tens.rileva(),
                "potenza_richiesta": 0
            }
        # Simula una richiesta di potenza basata sul veicolo
        max_potenza_veicolo = VEICOLI[self.veicolo]["max_potenza"]
        # Tende a richiedere il massimo all'inizio
        potenza_richiesta = random.uniform(max_potenza_veicolo * 0.5, max_potenza_veicolo)

        return {
            "id": self.id,
            "veicolo": self.veicolo,
            "stato": self.stato,
            "soc": self.soc_percento(),
            "temperatura": self.s_temp.rileva(),
            "temperatura_esterna": self.s_temp_ext.rileva(),
            "degrado": self.s_deg.rileva(),
            "tensione": self.s_tens.rileva(),
            "potenza_richiesta": round(potenza_richiesta, 1)
        }


# SERVER MULTI-COLONNINA
class StazioneServer:
    def analizza_colonnina(self, p):
        # Assumiamo che la potenza effettiva di partenza sia la richiesta
        potenza_effettiva = p["potenza_richiesta"]
        azioni = []

        # Le analisi sono rilevanti solo se c'è un veicolo in carica
        if p["stato"] != "OCCUPATA":
            return ["LIBERA"], 0

        temp = p["temperatura"]
        temp_ext = p["temperatura_esterna"]
        degrado = p["degrado"]
        veicolo = p["veicolo"]

        # Regolazione per Modalità Stazione
        if CONFIG["modalita"] == "Eco":
            potenza_effettiva *= 0.75
            azioni.append("MODALITA: Eco (-25%)")
        elif CONFIG["modalita"] == "Boost":
            potenza_effettiva *= 1.20
            azioni.append("MODALITA: Boost (+20%)")

        # Regolazione per Limite Veicolo
        if veicolo and potenza_effettiva > VEICOLI[veicolo]["max_potenza"]:
            potenza_effettiva = VEICOLI[veicolo]["max_potenza"]
            azioni.append("LIMITE: Veicolo Max")

        # Regolazione per Temperatura
        if temp is not None:
            if CONFIG["soglia_temp_alta"] < temp <= CONFIG["soglia_temp_critica"]:
                potenza_effettiva *= 0.50  # Riduzione del 50%
                azioni.append("RIDUCI: Temp Alta (-50%)")
            elif temp > CONFIG["soglia_temp_critica"]:
                potenza_effettiva = 0  # Azzeramento potenza
                azioni.append("FERMA: Temp Critica")

        # Regolazione per Degrado
        if degrado and degrado > CONFIG["soglia_degrado"]:
            potenza_effettiva = 0  # Azzeramento potenza
            azioni.append("FERMA: Degrado Alto")

        # Assicura che la potenza non sia negativa e non superi il massimo assoluto della colonnina
        potenza_effettiva = max(0, min(potenza_effettiva, CONFIG["max_potenza"]))

        if not azioni:
            azioni.append("OK")

        return azioni, round(potenza_effettiva, 1)

    def analizza_stazione(self, lista_parametri):
        # Considera solo la potenza EFFETTIVA erogata
        totale = sum(p["potenza_effettiva"] for p in lista_parametri if "potenza_effettiva" in p)
        alert = None

        if totale > CONFIG["potenza_massima_stazione"]:
            alert = f"SOVRACCARICO STAZIONE! Totale {totale:.1f} kW > {CONFIG['potenza_massima_stazione']} kW"

        return alert, round(totale, 1)


# AVVIO STAZIONE
def avvia_stazione(num_colonnine=4):
    colonnine = [Colonnina(i + 1) for i in range(num_colonnine)]
    server = StazioneServer()

    cicli_simulati = 10
    print(f"\n Avvio simulazione per {cicli_simulati} cicli. {num_colonnine} colonnine.")

    for ciclo in range(cicli_simulati):
        print(f"\n --- CICLO {ciclo + 1}/{cicli_simulati} ---")

        parametri_lista = []
        potenze_effettive = []

        for col in colonnine:
            if col.stato == "LIBERA":
                # La colonnina è libera e non c'è un'auto in attesa
                # 40% probabilità che arrivi un'auto in questo ciclo
                if random.random() < 0.4:
                    col.assegna_auto()
                else:
                    print(f" Colonnina {col.id} è LIBERA e in attesa.")
                    # Aggiungi parametri 'vuoti' per la telemetria anche se libera
                    parametri_lista.append(col.leggi_parametri())
                    continue  # Passa alla prossima colonnina

            if col.stato == "COMPLETATA":
                print(f" Colonnina {col.id} è stata liberata.")
                col.stato = "LIBERA"
                col.veicolo = None
                # Se è appena stata liberata, salta l'analisi e aspetta il prossimo ciclo
                continue

            # Lettura Sensori e Richiesta Potenza
            p = col.leggi_parametri()

            # Analisi Server
            azioni, potenza_effettiva = server.analizza_colonnina(p)

            # Aggiornamento Dati e Stato
            p["azioni"] = azioni
            p["potenza_effettiva"] = potenza_effettiva

            #Aggiornamento Carica (usa la potenza effettiva regolata!)
            col.aggiorna_soc(potenza_effettiva)

            # Aggiunta alla lista per l'analisi stazione
            parametri_lista.append(p)

            # Stampa un riepilogo per ciclo
            print(
                f"   Col. {col.id} ({p['veicolo']}): SoC {p['soc']}% | Potenza Eff. {potenza_effettiva} kW | Azioni: {', '.join(azioni)}")

            #PUBBLICA SUBTOPIC PER NODE-RED (Dati singoli colonnina)
            client.publish(f"ev/stazione/colonnina/{col.id}", json.dumps(p))

        #Analisi Totale Stazione
        alert, totale_carica = server.analizza_stazione(parametri_lista)

        server_data = {
            "timestamp": time.time(),
            "totale_carica_kw": totale_carica,
            "alert_stazione": alert,
            "modalita_stazione": CONFIG["modalita"]
        }

        # PUBBLICA TOPIC TELEMETRIA GENERALE
        client.publish(MQTT_TOPIC_TELEMETRY, json.dumps({"colonnine": parametri_lista}))
        # PUBBLICA TOPIC SERVER (Dati aggregati)
        client.publish(MQTT_TOPIC_SERVER, json.dumps(server_data))

        # Stampa l'esito dell'analisi aggregata
        if alert:
            print(f" ALERT STAZIONE: {alert}")
        print(f" Totale Carica Stazione: {totale_carica} kW")

        time.sleep(2)

    print("\n SIMULAZIONE COMPLETATA")
    client.loop_stop()
# MAIN
if __name__ == "__main__":
    # Puoi cambiare la modalità della stazione qui per testare Eco o Boost
    # CONFIG["modalita"] = "Eco"

    if login():
        avvia_stazione(num_colonnine=4)
