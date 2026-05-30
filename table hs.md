| Frequency / Range | Service | Mode | Type | Interest | Notes (local context) |
|---|---|---|---|---|---|
| **HF / shortwave (below 30 MHz)** |||||
| 2–30 MHz | Amateur radio HF bands | SSB, CW, digi | Listenable | ★★★ | International propagation — can hear distant stations. Key bands: 40m (7 MHz), 20m (14 MHz), 15m (21 MHz). Good from high ground around Annecy. |
| 26.965–27.405 MHz | CB radio | AM / FM | Listenable | ★★ | Still used by truckers and some rural workers in the Alps. Ch.9 = 27.065 MHz emergency. |
| **VHF Low (30–88 MHz)** |||||
| 68–88 MHz | Pompiers (analogue legacy) | NFM | Listenable | ★★★ | 85.500 & 86.600 MHz national fire frequencies still in partial use. Local SDIS 74 ops before ANTARES rollout. |
| 72.210–72.490 MHz | RC models (aeronautics) | OOK / PPM | Data/telemetry | ★ | French/Italian RC aircraft band. Short bursts when a transmitter is active nearby. |
| 72.600 MHz | Wildlife telemetry beacon | OOK CW | Data/telemetry | ★★★ | Your signal — identified. OFB/research fixed station. Visible on SDR as narrow spike. |
| 70–74 MHz | Other wildlife telemetry | OOK CW | Data/telemetry | ★★ | Other collared animals (chamois, lynx, wolves) may appear as sporadic pulse beacons in this range. Slow-scan the band. |
| 80–83 MHz | SAMU / ambulances | NFM | Listenable | ★★ | Legacy analogue. Some ops still on ~80 MHz in alpine areas before full ANTARES migration. |
| **FM Broadcast (88–108 MHz)** |||||
| 88–108 MHz | FM radio broadcast | WFM stereo | Listenable | ★ | Strong local signals from Swiss and French transmitters. RDS data decodable. DX propagation events (Sporadic-E in summer) can bring in Spanish/Italian stations. |
| **VHF High (108–174 MHz)** |||||
| 108–118 MHz | Aviation navigation (VOR/ILS) | AM / CW ident | Navigation | ★★★ | Chambéry VOR (CBY) ~114–115 MHz. Annecy NDB "AT". Morse idents audible at low volume. Continuous signal. |
| 118–137 MHz | Aviation voice (ATC) | AM | Aviation | ★★★★ | Annecy TWR: 118.200 MHz. Approach: 121.205 MHz. Guard: 121.500 MHz. Busy during ski season with medevac and private jets. Helicopters (PGHM, SAF) very active in summer. |
| 121.500 MHz | Aviation emergency guard | AM | Aviation | ★★★ | International distress frequency. Always monitored. ELT activations from aircraft accidents occasionally audible. |
| 143.9875 MHz | Vol libre / paragliding | NFM | Listenable | ★★★★ | FFLV national paragliding frequency. Very active around Annecy (Forclaz, Montmin, Semnoz). Pilots chat, report conditions, coordinate with instructors. |
| 150–154 MHz | Mountain rescue networks (GRA) | NFM | Listenable | ★★★★ | Grand Réseau Radio des Alpes. SAMB (Sécurité Alerte Mont-Blanc). 154.4625 MHz = 3 Mont-Blanc relays. Professional rescue comms, refuge alerts. |
| 156.800 MHz | Marine VHF ch.16 | NFM | Listenable | ★★ | Lac d'Annecy maritime traffic. Used by sailing clubs, rescue boats. Low activity but audible from the lakeshore. |
| 161.300 MHz | Canal Emergency (Canal E) | NFM + CTCSS 123 Hz | Listenable | ★★★★ | Transfrontier mountain emergency: Haute-Savoie (SDIS 74) + Valais (CH) + Val d'Aoste (IT). 33 relays covering Mont-Blanc massif. Monitored 24/7 from Annecy/Meythet. |
| **UHF (200–512 MHz)** |||||
| 380–410 MHz | INPT (TETRAPOL) — ACROPOL / RUBIS / ANTARES | TETRAPOL digital | Encrypted | ★★ | Police nationale (ACROPOL), Gendarmerie (RUBIS), Pompiers/SAMU (ANTARES). Encrypted voice + data. Identifiable by characteristic GMSK bursts on waterfall — can see activity even if you can't decode. |
| 406 MHz | EPIRB / PLB distress beacons | Digital burst | Data/telemetry | ★★ | 406 MHz PLB activations from hikers/climbers in distress. Decoded by COSPAS-SARSAT. Occasional in the Alps during accidents. |
| 430–440 MHz | Amateur radio UHF | NFM, DMR, fusion | Listenable | ★★★ | Local F4/HB9 amateur repeaters. Geneva/Annecy region well-covered. Analogue voice still common. Some DMR repeaters. |
| 457 kHz | Avalanche transceivers (DVA/ARVA) | CW / audio tone | Data/telemetry | ★★★ | Standard avalanche beacon frequency. With a sensitive LF receiver you can pick these up. Very short range but interesting in ski season. |
| 465.650 / 465.750 MHz | Plan Rouge UHF (mass casualty) | NFM | Digital | ★ | National mass casualty coordination channels. Rarely heard except during major incidents. |
| **SHF / microwave & satellites** |||||
| 1090 MHz | ADS-B aircraft transponders | Mode S / ADS-B | Data/telemetry | ★★★★★ | Every aircraft over the Alps transmits position, altitude, speed. Feed to FlightAware/FR24 with a simple dongle. Excellent coverage from Annecy due to surrounding terrain elevation. |
| 1575.42 MHz | GPS L1 | DSSS | Navigation | ★★ | GPS satellite signals. With a patch antenna and SDR you can receive raw GPS. Interesting for signal analysis. |
| 137.x MHz | NOAA/Meteor weather satellites | APT / LRPT | Data/telemetry | ★★★★ | NOAA APT at 137.5/137.9125 MHz — decode live weather images with WXtoImg. Meteor-M2 (LRPT) gives colour images. Good passes over the Alps several times/day. |