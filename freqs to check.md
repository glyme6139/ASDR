Paging (since you're already set up for it)

FLEX — more modern paging protocol, higher speed (1600/3200/6400 baud), used alongside POCSAG in many European networks. May still have some activity
ERMES — European Radio MEssaging System, actually designed and standardized in Europe, 169 MHz band. France was one of the main adopters. Likely mostly dead now but worth checking if there's any residual activity
DAPNET — amateur radio paging network, very much alive, runs POCSAG over amateur frequencies. Active community, you can even get your own RIC

Aviation (excellent in your area given proximity to Geneva, Lyon, Zurich)

VDL Mode 2 — digital datalink between aircraft and ground, 136 MHz band, carries ACARS messages over VHF. Very active
ACARS — older aircraft datalink, still heavily used, decodable with a simple VHF receiver. Tons of traffic over the Alps
ADS-B — you probably know this one already, 1090 MHz, but if not it's extremely satisfying to decode
ADS-C / HFDL — HF aircraft datalink, lets you receive transoceanic flights on shortwave

Railway

RaSTA / Eurobalise — but this is very hard to receive passively
GSM-R — railways use a dedicated GSM band (876-880 / 921-925 MHz). Voice and data for train control. You're right on the TGV corridor so there's plenty of it. Decodable with gr-gsm

Weather / Meteorology

RTTY weather fax — HF, still very much alive, NOAA and Météo-France both broadcast
SYNOP over RTTY — meteorological station reports on HF
Meteotsat LRIT/HRIT — satellite weather imagery from MSG satellites, needs a dish but very rewarding

Maritime (not far from Lac Léman / Rhône)

AIS — 161/162 MHz, vessel tracking, very easy to decode, active on Lac Léman
DSC — Digital Selective Calling on marine VHF, decoded alongside AIS

Utilities / Infrastructure

Linky teleinfo — French smart meters broadcast consumption data, decodable if you're near enough to a meter (your own is fine)
Wireless M-Bus — utility meters (gas, water, heat) across Europe, 868 MHz, lots of unencrypted data still out there
KNX RF — building automation, sometimes visible in apartment-dense areas
Sigfox — IoT network, 868 MHz, France is heavily covered, decodable frames though payload is usually encrypted

Time Signals

DCF77 — German time signal at 77.5 kHz, receivable easily from your location, very satisfying to decode the amplitude modulation
TDF — French time signal at 162 kHz, basically your local equivalent of DCF77

Trunked Radio

TETRA — digital trunked radio used by emergency services (SAMU, police, fire), very active in France. Decodable with osmo-tetra though voice is encrypted, metadata is not
DMR — some commercial and amateur use, partially decodable

Amateur

WSPR — HF propagation beacons, extremely weak signal, fascinating to see how far signals travel
FT8 — if you're not already on it, enormous activity
JS8Call — conversational FT8 variant

Your location near the Swiss border is particularly good for ACARS/VDL2 (Geneva airport), AIS (Lac Léman), and DCF77/TDF reception. What kind of receiver setup do you have?