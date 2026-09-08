#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 BOT TELEGRAM - RENOMMAGE + MINIATURE + PUBLICATION MULTI-CANAUX (v3)
 Édition complète : Panneau Admin, Plans/Quotas, Polices, Purge d'urgence
===============================================================================
 Bibliothèque   : Pyrogram (+ TgCrypto pour le chiffrement C++ natif ultra-rapide)
 Persistance    : MongoDB Atlas (via motor, driver asynchrone)
 Monitoring     : psutil (RAM / disque / CPU pour le panneau admin)
 Métadonnées    : hachoir (durée / largeur / hauteur réelles des vidéos)

 Installation :
     pip install pyrogram tgcrypto motor dnspython psutil hachoir

 Avant de lancer le script, renseigne dans la section CONFIGURATION :
   - API_ID / API_HASH (https://my.telegram.org)
   - BOT_TOKEN (via @BotFather)
   - MONGO_URI (ta chaîne de connexion MongoDB Atlas)
   - ADMIN_ID (ton user_id Telegram, via @userinfobot)

 Fonctionnalités principales :
   • Renommage + miniature + publication multi-canaux (jusqu'à 4)
   • Plans Gratuit / Basique / Standard / Pro avec quotas quotidiens
   • Sélecteur de police Unicode pour les légendes (/police)
   • Panneau admin : /stats, /broadcast, /addpremium
   • Commande d'urgence /clean (ou /flush) : purge totale du disque serveur

 Lancement :
     python bot_rename_poster_v3.py

 Compatible Thonny et VPS (Linux/Windows/Mac), Python 3.10+.
===============================================================================
"""

import os
import gc
import time
import shutil
import string
import asyncio
import logging
from datetime import datetime, timedelta, date

# ------------------------------------------------------------------------
# CORRECTIF DE COMPATIBILITÉ (Python 3.12+) :
# Pyrogram appelle en interne asyncio.get_event_loop() dès son import, ce qui
# plante sur les versions récentes de Python si aucune boucle asyncio n'existe
# encore sur le thread principal. On crée donc une boucle manuellement AVANT
# d'importer pyrogram.
# ------------------------------------------------------------------------
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, enums, idle
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.errors import (
    FloodWait,
    ChatAdminRequired,
    ChannelPrivate,
    UsernameNotOccupied,
    PeerIdInvalid,
    UserNotParticipant,
    RPCError,
)

from motor.motor_asyncio import AsyncIOMotorClient

# psutil est utilisé pour les statistiques serveur (RAM/disque) du panneau admin.
# pip install psutil
import psutil

# hachoir est utilisé pour extraire la durée, la largeur et la hauteur réelles
# des fichiers vidéo/audio, afin que Telegram affiche une vignette avec la
# bonne durée au lieu de rester bloqué sur 00:00.
# pip install hachoir
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata

# ==============================================================================
# 1. CONFIGURATION - À REMPLIR OBLIGATOIREMENT
# ==============================================================================
API_ID =   37198974                   # <-- Remplace par ton API_ID (int)
API_HASH = "ee486ee12aa06c1b33e245bfb34dd43b"       # <-- Remplace par ton API_HASH (str)
BOT_TOKEN = "8620294646:AAEA0amd3jaaNf2Zl5vf6X899YCzG27ScYo"     # <-- Remplace par le token donné par @BotFather

# Chaîne de connexion MongoDB Atlas, ex :
# "mongodb+srv://utilisateur:motdepasse@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority"
MONGO_URI = "mongodb+srv://altoftoure11_db_user:RXqlmZPy1XX4TdXs@cluster0.vbnmgxq.mongodb.net/?appName=Cluster0"
MONGO_DB_NAME = "altoftoure11_db_user"

# Nombre maximum de canaux qu'un utilisateur peut enregistrer
MAX_CANAUX = 4

# Identifiant Telegram (numérique) de l'administrateur du bot.
# Récupère le tien en écrivant à @userinfobot sur Telegram.
ADMIN_ID = 6572180966  # <-- Remplace par ton véritable user_id Telegram (int)
ADMIN_USERNAME = "altof3"        # Sans le @, utilisé dans les messages
CANAL_OFFICIEL = "https://t.me/botpload"

# Canal auquel l'utilisateur DOIT être abonné pour utiliser le bot.
CANAL_OBLIGATOIRE = "@botpload"                    # Username utilisé pour get_chat_member
CANAL_OBLIGATOIRE_LIEN = "https://t.me/botpload"    # Lien affiché dans le bouton "Rejoindre"

# Délai (en secondes) avant suppression automatique des fichiers envoyés à l'utilisateur
DELAI_SUPPRESSION_SECONDES = 900  # 15 minutes

# --------------------------------------------------------------------------
# GRILLE DES PLANS ET QUOTAS QUOTIDIENS (réinitialisés chaque jour)
# --------------------------------------------------------------------------
PLANS = {
    "gratuit": {"nom": "Gratuit", "emoji": "👤", "quota_go": 1, "prix": "0 FCFA"},
    "basique": {"nom": "Basique", "emoji": "🪙", "quota_go": 20, "prix": "🌎 50 étoiles / mois"},
    "standard": {"nom": "Standard", "emoji": "⚡", "quota_go": 50, "prix": "🌎 Étoiles / mois"},
    "pro": {"nom": "Pro", "emoji": "💎", "quota_go": 100, "prix": "🌎 Étoiles / mois"},
}
PLAN_PAR_DEFAUT = "gratuit"

# Nombre de connexions parallèles utilisées par Pyrogram pour le téléchargement
# et l'envoi de fichiers volumineux. Augmenter cette valeur accélère nettement
# le débit sur les gros fichiers (4 à 8 est un bon compromis pour un VPS).
MAX_CONCURRENT_TRANSMISSIONS = 4

# Dossiers de travail (créés automatiquement s'ils n'existent pas)
DOSSIER_DOWNLOADS = "downloads"
DOSSIER_THUMBS = "thumbnails"

for dossier in (DOSSIER_DOWNLOADS, DOSSIER_THUMBS):
    os.makedirs(dossier, exist_ok=True)

# ==============================================================================
# 2. LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot_rename_poster")

# ==============================================================================
# 3. STOCKAGE EN MÉMOIRE (cache rapide, synchronisé avec MongoDB)
# ==============================================================================
# Dictionnaire : user_id -> liste de dicts {"id": chat_id, "title": titre_canal}
user_channels: dict[int, list[dict]] = {}

# Dictionnaire : user_id -> {"file_path": chemin local, "file_id": id Telegram}
user_thumbnails: dict[int, dict] = {}

# Dictionnaire : user_id -> informations du fichier en attente de renommage
# Structure : {"message": Message, "type": "video"/"document"/"audio"}
pending_rename: dict[int, dict] = {}

# Dictionnaire : user_id -> document utilisateur complet (plan, quota, police, etc.)
# Sert de cache rapide, synchronisé avec la collection MongoDB "users".
users_cache: dict[int, dict] = {}

# Dictionnaire : user_id -> état intermédiaire du choix format/qualité, en attente
# que l'utilisateur clique sur les boutons inline correspondants.
# Structure : {"message_original": Message, "type_media": str, "nouveau_nom": str,
#              "format_choisi": str | None}
pending_traitement: dict[int, dict] = {}

# ==============================================================================
# 4. INITIALISATION DE MONGODB ATLAS (motor - driver asynchrone)
# ==============================================================================
mongo_client = AsyncIOMotorClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
collection_canaux = mongo_db["canaux"]          # un document par utilisateur
collection_thumbnails = mongo_db["thumbnails"]  # un document par utilisateur
collection_users = mongo_db["users"]            # un document par utilisateur (plan, quota, police...)
collection_stats = mongo_db["bot_stats"]        # un seul document global (_id="global")


async def mongo_sauvegarder_canaux(user_id: int):
    """Enregistre (upsert) la liste actuelle des canaux d'un utilisateur dans MongoDB."""
    try:
        await collection_canaux.update_one(
            {"_id": user_id},
            {"$set": {"canaux": user_channels.get(user_id, [])}},
            upsert=True,
        )
    except Exception as erreur:
        logger.warning(f"Échec de la sauvegarde MongoDB (canaux) pour {user_id} : {erreur}")


async def mongo_sauvegarder_thumbnail(user_id: int, file_id: str, file_path: str):
    """Enregistre (upsert) la miniature d'un utilisateur dans MongoDB."""
    try:
        await collection_thumbnails.update_one(
            {"_id": user_id},
            {"$set": {"file_id": file_id, "file_path": file_path}},
            upsert=True,
        )
    except Exception as erreur:
        logger.warning(f"Échec de la sauvegarde MongoDB (thumbnail) pour {user_id} : {erreur}")


async def mongo_supprimer_thumbnail(user_id: int):
    """Supprime le document de miniature d'un utilisateur dans MongoDB."""
    try:
        await collection_thumbnails.delete_one({"_id": user_id})
    except Exception as erreur:
        logger.warning(f"Échec de la suppression MongoDB (thumbnail) pour {user_id} : {erreur}")


async def mongo_charger_toutes_donnees(client: Client):
    """
    Recharge automatiquement, au démarrage du bot, les canaux et miniatures
    de tous les utilisateurs depuis MongoDB Atlas vers le cache en mémoire.
    Si une miniature n'existe plus localement sur le disque (redéploiement,
    changement de VPS, etc.), elle est retéléchargée depuis Telegram via
    son file_id, qui reste valide indéfiniment côté serveurs Telegram.
    """
    nb_canaux = 0
    async for doc in collection_canaux.find({}):
        user_channels[doc["_id"]] = doc.get("canaux", [])
        nb_canaux += 1

    nb_thumbs = 0
    async for doc in collection_thumbnails.find({}):
        user_id = doc["_id"]
        file_id = doc.get("file_id")
        file_path = doc.get("file_path")

        if file_path and not os.path.isfile(file_path) and file_id:
            # Le fichier local est absent : on le retélécharge depuis Telegram
            try:
                await client.download_media(file_id, file_name=file_path)
                logger.info(f"Miniature retéléchargée pour l'utilisateur {user_id}")
            except Exception as erreur:
                logger.warning(f"Impossible de retélécharger la miniature de {user_id} : {erreur}")
                continue

        user_thumbnails[user_id] = {"file_id": file_id, "file_path": file_path}
        nb_thumbs += 1

    nb_utilisateurs = await collection_users.count_documents({})

    logger.info(
        f"Données chargées depuis MongoDB Atlas : {nb_canaux} configuration(s) de canaux, "
        f"{nb_thumbs} miniature(s), {nb_utilisateurs} utilisateur(s) enregistré(s)."
    )


# ==============================================================================
# 4bis. GESTION DES UTILISATEURS, PLANS ET QUOTAS QUOTIDIENS (MongoDB)
# ==============================================================================

async def obtenir_ou_creer_utilisateur(user_id: int, username: str | None) -> dict:
    """
    Récupère le document utilisateur depuis le cache mémoire, ou depuis MongoDB
    s'il n'est pas encore en cache, ou le crée avec le plan Gratuit par défaut
    s'il n'existe nulle part (premier /start de l'utilisateur).
    """
    if user_id in users_cache:
        return users_cache[user_id]

    doc = await collection_users.find_one({"_id": user_id})
    if doc is None:
        doc = {
            "_id": user_id,
            "username": username,
            "plan": PLAN_PAR_DEFAUT,
            "plan_expiration": None,
            "usage_today_bytes": 0,
            "usage_date": date.today().isoformat(),
            "font": "normal",
            "date_inscription": datetime.utcnow(),
            "fichiers_traites": 0,
        }
        await collection_users.insert_one(doc)
        logger.info(f"Nouvel utilisateur enregistré : {user_id} ({username})")

    users_cache[user_id] = doc
    return doc


def _reinitialiser_quota_si_nouveau_jour(doc: dict) -> dict:
    """
    Réinitialise la consommation quotidienne si la date enregistrée dans le
    document ne correspond plus à aujourd'hui. Approche "à la demande" (plutôt
    qu'une tâche planifiée à minuit) : simple, fiable, et sans dépendance à un
    scheduler externe — le quota est simplement vérifié à chaque usage.
    """
    aujourd_hui = date.today().isoformat()
    if doc.get("usage_date") != aujourd_hui:
        doc["usage_date"] = aujourd_hui
        doc["usage_today_bytes"] = 0
    return doc


async def verifier_quota(user_id: int, taille_fichier_octets: int) -> tuple[bool, int, int]:
    """
    Vérifie si l'utilisateur dispose d'assez de quota quotidien restant pour
    traiter un fichier de la taille donnée.
    Retourne : (autorisé: bool, octets_restants: int, quota_total_octets: int)
    """
    doc = await obtenir_ou_creer_utilisateur(user_id, None)
    doc = _reinitialiser_quota_si_nouveau_jour(doc)

    plan = doc.get("plan", PLAN_PAR_DEFAUT)
    quota_octets = PLANS[plan]["quota_go"] * 1024 ** 3
    restant = quota_octets - doc.get("usage_today_bytes", 0)

    return (restant >= taille_fichier_octets), max(restant, 0), quota_octets


async def ajouter_usage(user_id: int, taille_octets: int):
    """
    Ajoute la taille d'un fichier traité à la consommation quotidienne de
    l'utilisateur, incrémente ses statistiques personnelles, et met à jour
    le compteur global de fichiers traités (persisté dans MongoDB).
    """
    doc = users_cache.get(user_id) or await obtenir_ou_creer_utilisateur(user_id, None)
    doc = _reinitialiser_quota_si_nouveau_jour(doc)

    doc["usage_today_bytes"] = doc.get("usage_today_bytes", 0) + taille_octets
    doc["fichiers_traites"] = doc.get("fichiers_traites", 0) + 1
    users_cache[user_id] = doc

    try:
        await collection_users.update_one(
            {"_id": user_id},
            {"$set": {
                "usage_today_bytes": doc["usage_today_bytes"],
                "usage_date": doc["usage_date"],
                "fichiers_traites": doc["fichiers_traites"],
            }},
            upsert=True,
        )
        await collection_stats.update_one(
            {"_id": "global"},
            {"$inc": {"total_fichiers_traites": 1}},
            upsert=True,
        )
    except Exception as erreur:
        logger.warning(f"Échec de la mise à jour MongoDB (usage) pour {user_id} : {erreur}")


# --------------------------------------------------------------------------
# SÉLECTEUR DE POLICE DE TEXTE (transformation Unicode du style d'affichage)
# --------------------------------------------------------------------------
# Ces fonctions convertissent un texte ASCII normal vers différents styles
# Unicode "esthétiques" (gras, italique, monospace, petites capitales,
# gothique, bulles) — utilisées uniquement pour la légende affichée, jamais
# pour le nom de fichier réel (qui doit rester compatible avec tous les
# systèmes d'exploitation et lecteurs multimédias).

def _construire_bloc_unicode(depart_maj: int, depart_min: int, depart_chiffre: int | None = None,
                              exceptions_maj: dict | None = None, exceptions_min: dict | None = None) -> dict:
    """Construit un dictionnaire de correspondance caractère -> caractère stylisé."""
    mappage = {}
    exceptions_maj = exceptions_maj or {}
    exceptions_min = exceptions_min or {}

    for i, c in enumerate(string.ascii_uppercase):
        mappage[c] = exceptions_maj.get(c, chr(depart_maj + i))
    for i, c in enumerate(string.ascii_lowercase):
        mappage[c] = exceptions_min.get(c, chr(depart_min + i))
    if depart_chiffre is not None:
        for i, c in enumerate(string.digits):
            mappage[c] = chr(depart_chiffre + i)

    return mappage


# Exceptions du bloc Fraktur (Gothique) : certaines lettres majuscules ne
# suivent pas la plage contiguë Unicode et utilisent des caractères historiques.
_EXCEPTIONS_GOTHIQUE = {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}

# Le bloc "Small Capital" (API phonétique) ne couvre pas tout l'alphabet ;
# on complète manuellement avec les caractères disponibles les plus proches.
_MAPPAGE_SMALLCAPS = {
    "A": "ᴀ", "B": "ʙ", "C": "ᴄ", "D": "ᴅ", "E": "ᴇ", "F": "ꜰ", "G": "ɢ", "H": "ʜ",
    "I": "ɪ", "J": "ᴊ", "K": "ᴋ", "L": "ʟ", "M": "ᴍ", "N": "ɴ", "O": "ᴏ", "P": "ᴘ",
    "Q": "ǫ", "R": "ʀ", "S": "s", "T": "ᴛ", "U": "ᴜ", "V": "ᴠ", "W": "ᴡ", "X": "x",
    "Y": "ʏ", "Z": "ᴢ",
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ", "h": "ʜ",
    "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ",
    "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ",
}

# Le bloc "Circled" (bulles) a un décalage différent pour les chiffres,
# et le zéro est un cas particulier (⓪ n'est pas contigu avec 1-9).
_MAPPAGE_BUBBLE_CHIFFRES = {"0": "⓪", "1": "①", "2": "②", "3": "③", "4": "④",
                             "5": "⑤", "6": "⑥", "7": "⑦", "8": "⑧", "9": "⑨"}

MAPPAGE_POLICES = {
    "normal": None,
    "bold": _construire_bloc_unicode(0x1D400, 0x1D41A, 0x1D7CE),
    "italic": _construire_bloc_unicode(0x1D434, 0x1D44E),
    "monospace": _construire_bloc_unicode(0x1D670, 0x1D68A, 0x1D7F6),
    "gothic": _construire_bloc_unicode(0x1D504, 0x1D51E, exceptions_maj=_EXCEPTIONS_GOTHIQUE),
    "smallcaps": _MAPPAGE_SMALLCAPS,
    "bubble": {**_construire_bloc_unicode(0x24B6, 0x24D0), **_MAPPAGE_BUBBLE_CHIFFRES},
}

NOMS_POLICES = {
    "normal": "🔤 Normal",
    "bold": "𝗕𝗼𝗹𝗱",
    "italic": "𝘐𝘵𝘢𝘭𝘪𝘤",
    "monospace": "𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎",
    "smallcaps": "ꜱᴍᴀʟʟ ᴄᴀᴘꜱ",
    "gothic": "𝔊𝔬𝔱𝔥𝔦𝔠",
    "bubble": "ⓑⓤⓑⓑⓛⓔ",
}


def convertir_police(texte: str, style: str) -> str:
    """Convertit un texte vers le style Unicode demandé (retombe sur le texte original si le style est inconnu)."""
    mappage = MAPPAGE_POLICES.get(style)
    if not mappage:
        return texte
    return "".join(mappage.get(c, c) for c in texte)


# ==============================================================================
# 4ter. ABONNEMENT OBLIGATOIRE AU CANAL
# ==============================================================================

async def verifier_abonnement_canal(client: Client, user_id: int) -> bool:
    """
    Vérifie si un utilisateur est membre du canal obligatoire via get_chat_member.
    Retourne True s'il est abonné, False sinon.

    En cas d'erreur technique (canal temporairement inaccessible, problème réseau),
    on choisit de NE PAS bloquer l'utilisateur pour éviter de rendre le bot
    totalement inutilisable à cause d'un souci indépendant de sa volonté — seul
    un "UserNotParticipant" explicite (l'utilisateur a réellement quitté ou n'a
    jamais rejoint) entraîne un blocage.
    """
    try:
        membre = await client.get_chat_member(CANAL_OBLIGATOIRE, user_id)
        return membre.status not in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED)
    except UserNotParticipant:
        return False
    except Exception as erreur:
        logger.warning(f"Impossible de vérifier l'abonnement de {user_id} au canal : {erreur}")
        return True


async def envoyer_message_abonnement_requis(client: Client, chat_id: int):
    """Envoie le message de blocage avec les boutons 'Rejoindre' et 'Vérifier'."""
    boutons = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Rejoindre le Canal", url=CANAL_OBLIGATOIRE_LIEN)],
        [InlineKeyboardButton("✅ Vérifier", callback_data="verifier_abonnement")],
    ])
    await client.send_message(
        chat_id,
        "🔒 **Accès restreint**\n\n"
        "Pour utiliser ce bot, tu dois d'abord rejoindre notre canal officiel :\n"
        f"{CANAL_OBLIGATOIRE_LIEN}\n\n"
        "Une fois abonné, clique sur **✅ Vérifier** ci-dessous.",
        reply_markup=boutons,
    )


# --------------------------------------------------------------------------
# CHOIX DE FORMAT ET DE QUALITÉ D'ENVOI (vidéos / documents vidéo)
# --------------------------------------------------------------------------
QUALITES_PREMIUM = {"4k", "8k"}
NOMS_QUALITES = {
    "480p": "⚡ Faible (480p)",
    "hd": "📺 HD (720p/1080p)",
    "4k": "👑 4K Ultra HD",
    "8k": "👑 8K Extreme HD",
}

EXTENSIONS_VIDEO = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts")


def est_document_video(message: Message) -> bool:
    """Détermine si un document envoyé est en réalité un fichier vidéo (par mime-type ou extension)."""
    document = message.document
    if not document:
        return False
    mime = (document.mime_type or "").lower()
    nom = (document.file_name or "").lower()
    return mime.startswith("video/") or nom.endswith(EXTENSIONS_VIDEO)


async def supprimer_message_apres_delai(client: Client, chat_id: int, message_id: int,
                                         delai_secondes: int = DELAI_SUPPRESSION_SECONDES):
    """
    Tâche asynchrone indépendante (lancée via asyncio.create_task) qui attend
    le délai indiqué puis supprime automatiquement le message envoyé. N'importe
    quelle erreur (message déjà supprimé manuellement, etc.) est journalisée
    sans jamais interrompre le reste du bot.
    """
    await asyncio.sleep(delai_secondes)
    try:
        await client.delete_messages(chat_id, message_id)
        logger.info(f"Message {message_id} supprimé automatiquement (délai écoulé) dans le chat {chat_id}")
    except Exception as erreur:
        logger.warning(f"Impossible de supprimer automatiquement le message {message_id} : {erreur}")


# ==============================================================================
# 5. INITIALISATION DU CLIENT PYROGRAM (avec optimisations de vitesse)
# ==============================================================================
app = Client(
    "bot_rename_poster_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    # TgCrypto est détecté et utilisé automatiquement par Pyrogram s'il est
    # installé (pip install tgcrypto) : chiffrement/déchiffrement en C++ natif.
    # max_concurrent_transmissions active plusieurs connexions parallèles pour
    # les transferts de fichiers volumineux, ce qui augmente le débit réel.
    max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
)

# ==============================================================================
# 6. FONCTIONS UTILITAIRES
# ==============================================================================

def formater_taille(taille_octets: float) -> str:
    """Convertit une taille en octets en une chaîne lisible (Ko, Mo, Go)."""
    for unite in ("o", "Ko", "Mo", "Go", "To"):
        if taille_octets < 1024:
            return f"{taille_octets:.2f} {unite}"
        taille_octets /= 1024
    return f"{taille_octets:.2f} Po"


def barre_progression(pourcentage: float, taille: int = 15) -> str:
    """Génère une barre de progression textuelle."""
    rempli = int(taille * pourcentage / 100)
    return "█" * rempli + "░" * (taille - rempli)


class SuiviProgression:
    """
    Classe qui affiche une progression en temps réel (pourcentage, vitesse en
    Mo/s, ETA) tout en limitant la fréquence de mise à jour du message pour
    éviter de déclencher un FloodWait (limite anti-spam de Telegram).
    """

    def __init__(self, message: Message, action: str):
        self.message = message
        self.action = action  # "Téléchargement" ou "Envoi"
        self.debut = time.time()
        self.dernier_edit = 0.0

    async def callback(self, actuel: int, total: int):
        maintenant = time.time()
        # Mise à jour toutes les ~3 secondes seulement (ou à la fin du transfert)
        if maintenant - self.dernier_edit < 3 and actuel != total:
            return
        self.dernier_edit = maintenant

        pourcentage = (actuel / total) * 100 if total else 0
        temps_ecoule = maintenant - self.debut
        vitesse_octets_par_sec = actuel / temps_ecoule if temps_ecoule > 0 else 0
        eta = (total - actuel) / vitesse_octets_par_sec if vitesse_octets_par_sec > 0 else 0

        texte = (
            f"**{self.action} en cours...** ⚡\n\n"
            f"[{barre_progression(pourcentage)}] {pourcentage:.1f}%\n\n"
            f"**Taille :** {formater_taille(actuel)} / {formater_taille(total)}\n"
            f"**Vitesse :** {formater_taille(vitesse_octets_par_sec)}/s\n"
            f"**Temps restant estimé :** {int(eta)}s"
        )
        try:
            await self.message.edit_text(texte)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            # On ignore les erreurs mineures d'édition (message trop souvent modifié, etc.)
            pass


def nettoyer_fichiers(*chemins: str):
    """
    Supprime en toute sécurité les fichiers temporaires donnés en argument.
    Chaque suppression est protégée individuellement : un échec sur un
    fichier n'empêche pas la suppression des autres.
    """
    for chemin in chemins:
        try:
            if chemin and os.path.isfile(chemin):
                os.remove(chemin)
                logger.info(f"Fichier temporaire supprimé : {chemin}")
        except Exception as erreur:
            logger.warning(f"Impossible de supprimer {chemin} : {erreur}")


def extraire_metadonnees_video(chemin_fichier: str) -> tuple[int, int, int]:
    """
    Extrait la durée réelle (en secondes), la largeur et la hauteur d'un
    fichier vidéo (ou audio) grâce à hachoir, pour que Telegram affiche une
    vignette avec la bonne durée au lieu de rester bloqué sur 00:00.

    Retourne toujours un tuple (duree, largeur, hauteur) : en cas d'échec
    d'analyse (fichier corrompu, format non reconnu, métadonnées absentes...),
    retourne (0, 0, 0) sans jamais lever d'exception — le bot continue de
    fonctionner normalement, simplement sans ces informations.
    """
    duree, largeur, hauteur = 0, 0, 0
    parseur = None

    try:
        parseur = createParser(chemin_fichier)
        if parseur is None:
            logger.warning(f"hachoir n'a pas pu analyser ce fichier : {chemin_fichier}")
            return duree, largeur, hauteur

        try:
            metadonnees = extractMetadata(parseur)
        except Exception as erreur:
            logger.warning(f"Échec de l'extraction des métadonnées ({chemin_fichier}) : {erreur}")
            metadonnees = None

        if metadonnees is not None:
            # Chaque métadonnée est lue individuellement : l'absence de l'une
            # d'entre elles (ex: pas de largeur/hauteur sur un fichier audio)
            # ne doit jamais empêcher la récupération des autres.
            if metadonnees.has("duration"):
                try:
                    duree = int(metadonnees.get("duration").total_seconds())
                except Exception:
                    pass
            if metadonnees.has("width"):
                try:
                    largeur = int(metadonnees.get("width"))
                except Exception:
                    pass
            if metadonnees.has("height"):
                try:
                    hauteur = int(metadonnees.get("height"))
                except Exception:
                    pass

    except Exception as erreur:
        # Filet de sécurité global : quoi qu'il arrive, on ne plante jamais le bot
        # à cause d'un problème d'extraction de métadonnées.
        logger.warning(f"Erreur inattendue lors de l'analyse hachoir ({chemin_fichier}) : {erreur}")

    finally:
        if parseur is not None:
            try:
                parseur.close()
            except Exception:
                pass

    return duree, largeur, hauteur


# ==============================================================================
# 7. COMMANDES DE BASE
# ==============================================================================

@app.on_message(filters.command("start") & filters.private)
async def commande_start(client: Client, message: Message):
    # Enregistrement (ou récupération) de l'utilisateur dans MongoDB avec le plan Gratuit par défaut
    doc = await obtenir_ou_creer_utilisateur(message.from_user.id, message.from_user.username)
    plan_actuel = PLANS.get(doc.get("plan", PLAN_PAR_DEFAUT), PLANS[PLAN_PAR_DEFAUT])

    await message.reply_text(
        "👋 **Bienvenue sur le Bot Rename & Post !**\n\n"
        "Voici ce que je peux faire :\n"
        "📸 Envoie-moi une photo pour définir ta **miniature personnalisée**.\n"
        "🎬 Envoie-moi une vidéo, un document ou un audio pour le **renommer** "
        "et le **publier automatiquement** sur tes canaux enregistrés.\n\n"
        "**Commandes disponibles :**\n"
        "/channel — Menu interactif pour connecter/déconnecter un canal (optionnel)\n"
        "/ajouter_canal @canal ou id — Ajouter un canal (max 4)\n"
        "/mes_canaux — Voir tes canaux enregistrés\n"
        "/supprimer_canal @canal ou id — Retirer un canal\n"
        "/view_thumb — Voir ta miniature actuelle\n"
        "/del_thumb — Supprimer ta miniature\n"
        "/police — Choisir le style de police de tes légendes\n"
        "/myplan — Voir ton plan et ta consommation du jour\n"
        "/plans — Voir la grille tarifaire complète\n"
        "/aide — Afficher ce message\n\n"
        f"{plan_actuel['emoji']} Ton plan actuel : **{plan_actuel['nom']}** "
        f"({plan_actuel['quota_go']} Go / jour)\n\n"
        "💾 Tes canaux, ta miniature et tes préférences sont sauvegardés de façon "
        "permanente (MongoDB Atlas) : ils seront toujours disponibles même après "
        "un redémarrage du bot."
    )


@app.on_message(filters.command("aide") & filters.private)
async def commande_aide(client: Client, message: Message):
    await commande_start(client, message)


@app.on_message(filters.command("myplan") & filters.private)
async def commande_myplan(client: Client, message: Message):
    """Affiche le plan actuel, le quota, la consommation du jour et l'expiration."""
    user_id = message.from_user.id
    doc = await obtenir_ou_creer_utilisateur(user_id, message.from_user.username)
    doc = _reinitialiser_quota_si_nouveau_jour(doc)

    plan = doc.get("plan", PLAN_PAR_DEFAUT)
    infos_plan = PLANS[plan]
    quota_octets = infos_plan["quota_go"] * 1024 ** 3
    utilise = doc.get("usage_today_bytes", 0)
    restant = max(quota_octets - utilise, 0)

    expiration = doc.get("plan_expiration")
    texte_expiration = expiration.strftime("%d/%m/%Y") if expiration else "Illimité (plan gratuit)"

    await message.reply_text(
        f"{infos_plan['emoji']} **Ton plan actuel : {infos_plan['nom']}**\n\n"
        f"📦 Quota quotidien : {infos_plan['quota_go']} Go\n"
        f"📊 Utilisé aujourd'hui : {formater_taille(utilise)}\n"
        f"🟢 Restant aujourd'hui : {formater_taille(restant)}\n"
        f"📅 Expiration : {texte_expiration}\n\n"
        "Utilise /plans pour voir la grille tarifaire complète."
    )


@app.on_message(filters.command("plans") & filters.private)
async def commande_plans(client: Client, message: Message):
    """Affiche la grille tarifaire complète avec les informations de paiement."""
    texte = (
        "╔════════════════════════════╗\n"
        f"👤 Utilisateur Plan {PLANS['gratuit']['nom']}\n"
        f"├ Limite De Destination Quotidienne : {PLANS['gratuit']['quota_go']} Go\n"
        f"└ Prix : {PLANS['gratuit']['prix']}\n\n"
        f"{PLANS['basique']['emoji']} {PLANS['basique']['nom']}\n"
        f"├ Limite De Destination Quotidienne : {PLANS['basique']['quota_go']} Go\n"
        f"└ Prix : {PLANS['basique']['prix']}\n\n"
        f"{PLANS['standard']['emoji']} {PLANS['standard']['nom']}\n"
        f"├ Limite De Destination Quotidienne : {PLANS['standard']['quota_go']} Go\n"
        f"└ Prix : {PLANS['standard']['prix']}\n\n"
        f"{PLANS['pro']['emoji']} {PLANS['pro']['nom']}\n"
        f"├ Limite De Destination Quotidienne : {PLANS['pro']['quota_go']} Go\n"
        f"└ Prix : {PLANS['pro']['prix']}\n\n"
        "💎 Échanger vos étoiles Telegram pour avoir des plans Premium\n"
        f"Après paiement, envoyez une Capture De Le Payement Du Payeur à L'admin @{ADMIN_USERNAME}.\n\n"
        f"📢 Canal officiel : {CANAL_OFFICIEL}\n"
        "╚════════════════════════════╝"
    )
    await message.reply_text(texte)


@app.on_message(filters.command(["police", "font"]) & filters.private)
async def commande_police(client: Client, message: Message):
    """Affiche un menu interactif pour choisir le style de police des légendes."""
    boutons = [
        [
            InlineKeyboardButton(NOMS_POLICES["normal"], callback_data="font_normal"),
            InlineKeyboardButton(NOMS_POLICES["bold"], callback_data="font_bold"),
        ],
        [
            InlineKeyboardButton(NOMS_POLICES["italic"], callback_data="font_italic"),
            InlineKeyboardButton(NOMS_POLICES["monospace"], callback_data="font_monospace"),
        ],
        [
            InlineKeyboardButton(NOMS_POLICES["smallcaps"], callback_data="font_smallcaps"),
            InlineKeyboardButton(NOMS_POLICES["gothic"], callback_data="font_gothic"),
        ],
        [
            InlineKeyboardButton(NOMS_POLICES["bubble"], callback_data="font_bubble"),
        ],
    ]
    await message.reply_text(
        "🎨 **Choisis le style de police pour la légende de tes fichiers :**",
        reply_markup=InlineKeyboardMarkup(boutons),
    )


@app.on_callback_query(filters.regex(r"^font_"))
async def callback_selection_police(client: Client, callback_query: CallbackQuery):
    """Traite le clic sur un bouton du menu de police et sauvegarde le choix dans MongoDB."""
    user_id = callback_query.from_user.id
    style = callback_query.data.split("_", 1)[1]

    if style not in MAPPAGE_POLICES:
        await callback_query.answer("❌ Style inconnu.", show_alert=True)
        return

    doc = await obtenir_ou_creer_utilisateur(user_id, callback_query.from_user.username)
    doc["font"] = style
    users_cache[user_id] = doc

    try:
        await collection_users.update_one(
            {"_id": user_id}, {"$set": {"font": style}}, upsert=True
        )
    except Exception as erreur:
        logger.warning(f"Échec de la sauvegarde MongoDB (police) pour {user_id} : {erreur}")

    await callback_query.answer(f"Police définie : {NOMS_POLICES[style]} ✅")
    exemple = convertir_police("Exemple Naruto Episode 01", style) if style != "normal" else "Exemple Naruto Episode 01"
    await callback_query.message.edit_text(
        f"✅ **Police sélectionnée : {NOMS_POLICES[style]}**\n\n"
        f"Aperçu : {exemple}\n\n"
        "Elle sera appliquée automatiquement à la légende de tes prochains fichiers."
    )


# ==============================================================================
# 8. GESTION DES CANAUX (jusqu'à 4, persistés dans MongoDB)
# ==============================================================================

def canal_est_valide(chat) -> bool:
    """
    Valide qu'une cible est bien un CANAL Telegram (et uniquement un canal —
    ni un groupe, ni un utilisateur, ni un bot). C'est la garantie que l'ID
    stocké est un véritable ID de destination de diffusion (typiquement au
    format -100xxxxxxxxxx) et jamais un chat_id de bot ou d'utilisateur privé,
    qui ferait planter send_video/send_document/send_audio si on tentait d'y
    publier.
    """
    return chat.type == enums.ChatType.CHANNEL


async def tenter_ajout_canal(client: Client, user_id: int, cible: str) -> str:
    """
    Logique commune d'ajout d'un canal, utilisée à la fois par la commande
    /ajouter_canal et par le flux interactif déclenché depuis /channel.
    Retourne le texte de réponse à afficher à l'utilisateur.
    """
    canaux_actuels = user_channels.setdefault(user_id, [])

    if len(canaux_actuels) >= MAX_CANAUX:
        return (
            f"❌ Tu as déjà atteint la limite de **{MAX_CANAUX} canaux**. "
            f"Supprime-en un avant d'en ajouter un nouveau."
        )

    try:
        chat = await client.get_chat(cible)

        # Validation stricte : uniquement un canal, jamais un groupe/bot/utilisateur
        if not canal_est_valide(chat):
            return (
                "❌ Ce n'est pas un **canal** Telegram valide (groupe, bot ou "
                "utilisateur détecté). Fournis le lien, le @username ou l'ID "
                "(`-100xxxxxxxxxx`) d'un canal de diffusion."
            )

        # Vérifier que le bot est bien administrateur du canal avec droit de publier
        membre_bot = await client.get_chat_member(chat.id, "me")
        if membre_bot.status not in (
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ):
            return (
                "❌ Je ne suis pas administrateur de ce canal. "
                "Ajoute-moi comme **administrateur** avec le droit de publier des messages, "
                "puis réessaie."
            )

        if any(c["id"] == chat.id for c in canaux_actuels):
            return "ℹ️ Ce canal est déjà enregistré."

        # Double vérification du format d'ID avant stockage (défense en profondeur)
        if not str(chat.id).startswith("-100"):
            return "❌ Format d'ID de canal invalide (attendu : `-100xxxxxxxxxx`)."

        canaux_actuels.append({"id": chat.id, "title": chat.title or str(chat.id)})
        await mongo_sauvegarder_canaux(user_id)  # persistance immédiate dans Atlas

        return (
            f"✅ Canal **{chat.title}** ajouté avec succès "
            f"({len(canaux_actuels)}/{MAX_CANAUX}).\n"
            f"💾 Sauvegardé de façon permanente dans MongoDB Atlas."
        )

    except (ChannelPrivate, UsernameNotOccupied, PeerIdInvalid):
        return (
            "❌ Canal introuvable. Vérifie le nom d'utilisateur ou l'identifiant, "
            "et assure-toi que je suis bien membre du canal."
        )
    except ChatAdminRequired:
        return "❌ Je dois être administrateur de ce canal pour l'ajouter."
    except RPCError as erreur:
        return f"❌ Erreur Telegram : `{erreur}`"


@app.on_message(filters.command("ajouter_canal") & filters.private)
async def ajouter_canal(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(
            "⚠️ Utilisation : `/ajouter_canal @nom_du_canal` ou `/ajouter_canal -100xxxxxxxxxx`"
        )
        return

    resultat = await tenter_ajout_canal(client, message.from_user.id, message.command[1])
    await message.reply_text(resultat)


@app.on_message(filters.command("supprimer_canal") & filters.private)
async def supprimer_canal(client: Client, message: Message):
    user_id = message.from_user.id
    canaux_actuels = user_channels.get(user_id, [])

    if len(message.command) < 2:
        await message.reply_text("⚠️ Utilisation : `/supprimer_canal @nom_du_canal`")
        return

    cible = message.command[1]

    try:
        chat = await client.get_chat(cible)
        avant = len(canaux_actuels)
        user_channels[user_id] = [c for c in canaux_actuels if c["id"] != chat.id]

        if len(user_channels[user_id]) < avant:
            await mongo_sauvegarder_canaux(user_id)  # persistance immédiate dans Atlas
            await message.reply_text(f"✅ Canal **{chat.title}** retiré de ta liste.")
        else:
            await message.reply_text("ℹ️ Ce canal n'était pas enregistré.")
    except RPCError as erreur:
        await message.reply_text(f"❌ Erreur Telegram : `{erreur}`")


def texte_liste_canaux(user_id: int) -> str:
    """Construit le texte listant les canaux enregistrés d'un utilisateur (canal optionnel : liste vide autorisée)."""
    canaux_actuels = user_channels.get(user_id, [])
    if not canaux_actuels:
        return (
            "📭 Aucun canal connecté.\n\n"
            "C'est optionnel : sans canal, tes fichiers renommés sont simplement "
            "envoyés dans ce tchat privé. Connecte un canal si tu veux qu'ils y "
            "soient aussi publiés automatiquement."
        )
    texte = f"📡 **Canaux connectés ({len(canaux_actuels)}/{MAX_CANAUX}) :**\n\n"
    for i, canal in enumerate(canaux_actuels, start=1):
        texte += f"{i}. {canal['title']} (`{canal['id']}`)\n"
    return texte


@app.on_message(filters.command("mes_canaux") & filters.private)
async def mes_canaux(client: Client, message: Message):
    await message.reply_text(texte_liste_canaux(message.from_user.id))


# --------------------------------------------------------------------------
# /channel — INTERFACE INTERACTIVE DE CONNEXION DE CANAL (boutons)
# --------------------------------------------------------------------------
# Dictionnaire : user_id -> True si le bot attend que l'utilisateur envoie
# le @username / lien / ID du canal à connecter (déclenché depuis /channel).
pending_ajout_canal: dict[int, bool] = {}


@app.on_message(filters.command("channel") & filters.private)
async def commande_channel(client: Client, message: Message):
    """Menu interactif de configuration du canal de publication (connexion optionnelle)."""
    user_id = message.from_user.id
    canaux_actuels = user_channels.get(user_id, [])

    boutons = [[InlineKeyboardButton("➕ Connecter un canal", callback_data="canal_ajouter")]]
    if canaux_actuels:
        boutons.append([InlineKeyboardButton("🗑️ Déconnecter un canal", callback_data="canal_retirer_menu")])
    boutons.append([InlineKeyboardButton("📋 Voir mes canaux", callback_data="canal_liste")])

    await message.reply_text(
        "⚙️ **Configuration du canal de publication**\n\n"
        + texte_liste_canaux(user_id)
        + "\n\nLa connexion d'un canal est **entièrement optionnelle** : "
        "sans canal connecté, tes fichiers sont simplement envoyés dans ce tchat.",
        reply_markup=InlineKeyboardMarkup(boutons),
    )


@app.on_callback_query(filters.regex(r"^canal_liste$"))
async def callback_canal_liste(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()
    await callback_query.message.edit_text(texte_liste_canaux(callback_query.from_user.id))


@app.on_callback_query(filters.regex(r"^canal_ajouter$"))
async def callback_canal_ajouter(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    pending_ajout_canal[user_id] = True
    await callback_query.answer()
    await callback_query.message.edit_text(
        "✏️ **Envoie-moi maintenant** le lien, le @username ou l'ID (`-100xxxxxxxxxx`) "
        "du canal à connecter.\n\n"
        "⚠️ Assure-toi de m'avoir ajouté comme **administrateur** du canal au préalable.\n\n"
        "Tape /annuler pour annuler."
    )


@app.on_callback_query(filters.regex(r"^canal_retirer_menu$"))
async def callback_canal_retirer_menu(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    canaux_actuels = user_channels.get(user_id, [])

    if not canaux_actuels:
        await callback_query.answer("📭 Aucun canal à déconnecter.", show_alert=True)
        return

    boutons = [
        [InlineKeyboardButton(f"🗑️ {canal['title']}", callback_data=f"canal_retirer_{canal['id']}")]
        for canal in canaux_actuels
    ]
    await callback_query.answer()
    await callback_query.message.edit_text(
        "Sélectionne le canal à déconnecter :",
        reply_markup=InlineKeyboardMarkup(boutons),
    )


@app.on_callback_query(filters.regex(r"^canal_retirer_-?\d+$"))
async def callback_canal_retirer_confirmer(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    chat_id_cible = int(callback_query.data.split("_")[-1])

    canaux_actuels = user_channels.get(user_id, [])
    avant = len(canaux_actuels)
    canal_supprime = next((c for c in canaux_actuels if c["id"] == chat_id_cible), None)
    user_channels[user_id] = [c for c in canaux_actuels if c["id"] != chat_id_cible]

    if len(user_channels[user_id]) < avant:
        await mongo_sauvegarder_canaux(user_id)
        await callback_query.answer(f"✅ Canal déconnecté : {canal_supprime['title']}", show_alert=True)
        await callback_query.message.edit_text(texte_liste_canaux(user_id))
    else:
        await callback_query.answer("ℹ️ Ce canal n'était pas connecté.", show_alert=True)


def filtre_attente_ajout_canal(_, __, message: Message) -> bool:
    """Filtre : vrai si le bot attend un @username/ID de canal de la part de cet utilisateur."""
    return bool(message.from_user) and message.from_user.id in pending_ajout_canal


filtre_ajout_canal = filters.create(filtre_attente_ajout_canal)


@app.on_message(filters.text & filters.private & filtre_ajout_canal & ~filters.command("annuler"))
async def traiter_ajout_canal_interactif(client: Client, message: Message):
    """Réceptionne le @username/lien/ID envoyé après un clic sur '➕ Connecter un canal'."""
    user_id = message.from_user.id
    pending_ajout_canal.pop(user_id, None)

    cible = message.text.strip()
    resultat = await tenter_ajout_canal(client, user_id, cible)
    await message.reply_text(resultat)



# ==============================================================================
# 9. GESTION DE LA MINIATURE (THUMBNAIL, persistée dans MongoDB)
# ==============================================================================

@app.on_message(filters.photo & filters.private)
async def enregistrer_thumbnail(client: Client, message: Message):
    user_id = message.from_user.id
    chemin_thumb = os.path.join(DOSSIER_THUMBS, f"{user_id}.jpg")

    # Supprimer l'ancienne miniature locale si elle existe (nettoyage disque)
    ancienne = user_thumbnails.get(user_id, {})
    nettoyer_fichiers(ancienne.get("file_path"))

    try:
        await message.download(file_name=chemin_thumb)
        file_id = message.photo.file_id

        user_thumbnails[user_id] = {"file_id": file_id, "file_path": chemin_thumb}
        await mongo_sauvegarder_thumbnail(user_id, file_id, chemin_thumb)  # persistance Atlas

        await message.reply_text(
            "✅ **Miniature enregistrée avec succès !**\n"
            "Elle sera automatiquement appliquée à tes prochains fichiers renommés.\n"
            "💾 Sauvegardée de façon permanente dans MongoDB Atlas."
        )
    except Exception as erreur:
        await message.reply_text(f"❌ Erreur lors de l'enregistrement de la miniature : `{erreur}`")


@app.on_message(filters.command("view_thumb") & filters.private)
async def voir_thumbnail(client: Client, message: Message):
    user_id = message.from_user.id
    infos_thumb = user_thumbnails.get(user_id)
    chemin_thumb = infos_thumb.get("file_path") if infos_thumb else None

    if chemin_thumb and os.path.isfile(chemin_thumb):
        await message.reply_photo(chemin_thumb, caption="🖼️ Voici ta miniature actuelle.")
    else:
        await message.reply_text("📭 Tu n'as pas encore de miniature enregistrée.")


@app.on_message(filters.command("del_thumb") & filters.private)
async def supprimer_thumbnail(client: Client, message: Message):
    user_id = message.from_user.id
    infos_thumb = user_thumbnails.pop(user_id, None)

    if infos_thumb:
        nettoyer_fichiers(infos_thumb.get("file_path"))  # nettoyage disque
        await mongo_supprimer_thumbnail(user_id)          # persistance Atlas
        await message.reply_text("🗑️ Miniature supprimée avec succès (disque + base de données).")
    else:
        await message.reply_text("ℹ️ Tu n'avais pas de miniature enregistrée.")


# ==============================================================================
# 9bis. PANNEAU ADMINISTRATEUR (réservé à ADMIN_ID)
# ==============================================================================

def _taille_dossier(chemin: str) -> int:
    """Calcule récursivement la taille totale (en octets) d'un dossier."""
    total = 0
    for racine, _, fichiers in os.walk(chemin):
        for f in fichiers:
            try:
                total += os.path.getsize(os.path.join(racine, f))
            except OSError:
                pass
    return total


@app.on_message(filters.command(["clean", "flush"]) & filters.user(ADMIN_ID) & filters.private)
async def commande_purge_totale(client: Client, message: Message):
    """
    COMMANDE D'URGENCE (ADMIN UNIQUEMENT) :
    Purge immédiatement tous les fichiers temporaires du serveur (téléchargements
    et miniatures), force un garbage collection Python, puis restaure les
    miniatures essentielles depuis MongoDB/Telegram pour que le bot reste
    pleinement fonctionnel sans nécessiter de redémarrage.
    """
    message_statut = await message.reply_text("🧹 **Purge totale du serveur en cours...**")

    try:
        # a) Calcul de l'espace occupé AVANT purge, puis suppression complète
        taille_avant = _taille_dossier(DOSSIER_DOWNLOADS) + _taille_dossier(DOSSIER_THUMBS)

        for dossier in (DOSSIER_DOWNLOADS, DOSSIER_THUMBS):
            shutil.rmtree(dossier, ignore_errors=True)
            os.makedirs(dossier, exist_ok=True)

        # Le cache mémoire des miniatures pointe désormais vers des fichiers
        # supprimés : on vide le cache, il sera régénéré au prochain accès.
        user_thumbnails.clear()

        # b) Libération forcée de la mémoire RAM
        objets_liberes = gc.collect()

        # Restauration immédiate des miniatures essentielles depuis Telegram
        # (via leur file_id stocké dans MongoDB) pour ne pas casser le service.
        await mongo_charger_toutes_donnees(client)

        # c) Rapport précis à l'admin
        taille_apres = _taille_dossier(DOSSIER_DOWNLOADS) + _taille_dossier(DOSSIER_THUMBS)

        await message_statut.edit_text(
            "✅ **Purge totale terminée avec succès !**\n\n"
            f"🗑️ Espace disque libéré : {formater_taille(taille_avant)}\n"
            f"♻️ Objets Python libérés (gc.collect) : {objets_liberes}\n"
            f"📁 Espace actuellement occupé : {formater_taille(taille_apres)}\n\n"
            "ℹ️ Les miniatures des utilisateurs ont été automatiquement "
            "restaurées depuis Telegram (aucune perte de configuration)."
        )
        logger.info(f"Purge totale exécutée par l'admin {message.from_user.id}")

    except Exception as erreur:
        logger.exception("Erreur pendant la purge totale")
        await message_statut.edit_text(f"❌ Erreur pendant la purge : `{erreur}`")


@app.on_message(filters.command("stats") & filters.user(ADMIN_ID) & filters.private)
async def commande_stats(client: Client, message: Message):
    """Affiche les statistiques serveur en temps réel : utilisateurs, fichiers, RAM, disque, ping."""
    t0 = time.time()
    message_statut = await message.reply_text("📊 Calcul des statistiques...")
    ping_ms = (time.time() - t0) * 1000

    try:
        nb_utilisateurs = await collection_users.count_documents({})
        doc_stats = await collection_stats.find_one({"_id": "global"}) or {}
        total_fichiers = doc_stats.get("total_fichiers_traites", 0)

        ram = psutil.virtual_memory()
        disque = psutil.disk_usage(".")

        await message_statut.edit_text(
            "📊 **Statistiques du bot**\n\n"
            f"👥 Utilisateurs enregistrés : {nb_utilisateurs}\n"
            f"📁 Fichiers traités (total) : {total_fichiers}\n"
            f"🏓 Ping : {ping_ms:.1f} ms\n\n"
            f"💾 RAM : {formater_taille(ram.used)} / {formater_taille(ram.total)} ({ram.percent}%)\n"
            f"💽 Disque : {formater_taille(disque.used)} / {formater_taille(disque.total)} ({disque.percent}%)\n"
            f"🖥️ CPU : {psutil.cpu_percent(interval=0.5)}%"
        )
    except Exception as erreur:
        await message_statut.edit_text(f"❌ Erreur lors du calcul des statistiques : `{erreur}`")


@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID) & filters.private)
async def commande_broadcast(client: Client, message: Message):
    """
    Diffuse un message à tous les utilisateurs enregistrés dans MongoDB.
    Utilisation : /broadcast <texte>  —  ou répondre à un message avec /broadcast
    pour rediffuser ce message (texte, photo, vidéo...) tel quel.
    """
    message_source = message.reply_to_message
    texte_broadcast = message.text.split(None, 1)[1] if len(message.command) >= 2 else None

    if not message_source and not texte_broadcast:
        await message.reply_text(
            "⚠️ Utilisation : `/broadcast <texte>` ou réponds à un message avec `/broadcast`."
        )
        return

    message_statut = await message.reply_text("📢 Diffusion en cours...")
    succes, echecs = 0, 0

    async for doc in collection_users.find({}, {"_id": 1}):
        cible = doc["_id"]
        try:
            if message_source:
                await message_source.copy(chat_id=cible)
            else:
                await client.send_message(cible, texte_broadcast)
            succes += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                if message_source:
                    await message_source.copy(chat_id=cible)
                else:
                    await client.send_message(cible, texte_broadcast)
                succes += 1
            except Exception:
                echecs += 1
        except Exception:
            echecs += 1

        await asyncio.sleep(0.05)  # légère pause anti-flood entre chaque envoi

    await message_statut.edit_text(
        f"✅ **Diffusion terminée.**\n\n📨 Succès : {succes}\n❌ Échecs : {echecs}"
    )


@app.on_message(filters.command("addpremium") & filters.user(ADMIN_ID) & filters.private)
async def commande_addpremium(client: Client, message: Message):
    """
    Active un plan payant pour un utilisateur donné (après vérification manuelle
    du paiement par étoiles Telegram).
    Utilisation : /addpremium <user_id> <plan>
    """
    if len(message.command) < 3:
        await message.reply_text(
            "⚠️ Utilisation : `/addpremium <user_id> <plan>`\n"
            f"Plans valides : {', '.join(PLANS.keys())}"
        )
        return

    try:
        cible_id = int(message.command[1])
    except ValueError:
        await message.reply_text("❌ `user_id` invalide (doit être un nombre).")
        return

    plan = message.command[2].lower()
    if plan not in PLANS:
        await message.reply_text(f"❌ Plan inconnu. Choix valides : {', '.join(PLANS.keys())}")
        return

    doc = await obtenir_ou_creer_utilisateur(cible_id, None)
    expiration = (datetime.utcnow() + timedelta(days=30)) if plan != "gratuit" else None

    doc["plan"] = plan
    doc["plan_expiration"] = expiration
    users_cache[cible_id] = doc

    try:
        await collection_users.update_one(
            {"_id": cible_id},
            {"$set": {"plan": plan, "plan_expiration": expiration}},
            upsert=True,
        )
    except Exception as erreur:
        await message.reply_text(f"❌ Erreur lors de la mise à jour MongoDB : `{erreur}`")
        return

    infos_plan = PLANS[plan]
    texte_expiration = f" jusqu'au {expiration.strftime('%d/%m/%Y')}" if expiration else ""
    await message.reply_text(
        f"✅ Utilisateur `{cible_id}` passé au plan **{infos_plan['nom']}**{texte_expiration}."
    )

    # Notifier l'utilisateur concerné (échec silencieux s'il a bloqué le bot)
    try:
        await client.send_message(
            cible_id,
            f"🎉 **Ton plan a été mis à niveau vers {infos_plan['nom']} {infos_plan['emoji']} !**\n\n"
            f"Nouvelle limite quotidienne : {infos_plan['quota_go']} Go.\n"
            "Utilise /myplan pour voir le détail.",
        )
    except Exception:
        pass


# ==============================================================================
# 10. RÉCEPTION D'UN FICHIER (VIDÉO / DOCUMENT / AUDIO) -> DEMANDE DE NOM
# ==============================================================================

@app.on_message((filters.video | filters.document | filters.audio) & filters.private)
async def reception_fichier(client: Client, message: Message):
    user_id = message.from_user.id

    if message.video:
        type_media = "video"
    elif message.audio:
        type_media = "audio"
    else:
        type_media = "document"

    pending_rename[user_id] = {"message": message, "type": type_media}

    nom_actuel = (
        message.video.file_name if message.video and message.video.file_name else
        message.audio.file_name if message.audio and message.audio.file_name else
        message.document.file_name if message.document and message.document.file_name else
        "fichier_sans_nom"
    )

    await message.reply_text(
        f"📄 Fichier reçu : `{nom_actuel}`\n\n"
        "✏️ **Envoie-moi maintenant le nouveau nom du fichier** "
        "(avec son extension, ex: `[Anime] Naruto Episode 01.mp4`).\n\n"
        "Tape /annuler pour annuler cette opération."
    )


@app.on_message(filters.command("annuler") & filters.private)
async def annuler_operation(client: Client, message: Message):
    user_id = message.from_user.id
    annule = bool(
        pending_rename.pop(user_id, None)
        or pending_traitement.pop(user_id, None)
        or pending_ajout_canal.pop(user_id, None)
    )
    if annule:
        await message.reply_text("🚫 Opération annulée.")
    else:
        await message.reply_text("ℹ️ Aucune opération en cours.")


# ==============================================================================
# 11. RÉCEPTION DU NOUVEAU NOM -> TÉLÉCHARGEMENT, RENOMMAGE, ENVOI, PUBLICATION
# ==============================================================================

def filtre_attente_renommage(_, __, message: Message) -> bool:
    """Filtre personnalisé : vrai uniquement si l'utilisateur a un fichier en attente."""
    return bool(message.from_user) and message.from_user.id in pending_rename


filtre_renommage = filters.create(filtre_attente_renommage)


@app.on_message(filters.text & filters.private & filtre_renommage & ~filters.command("annuler"))
async def traiter_renommage(client: Client, message: Message):
    """
    Réceptionne le nouveau nom saisi par l'utilisateur. Pour une vidéo (ou un
    document identifié comme vidéo), propose ensuite le choix du format et de
    la qualité via des boutons inline. Pour un audio ou un document classique
    (non vidéo), le traitement démarre directement (comportement historique).
    """
    user_id = message.from_user.id
    infos = pending_rename.pop(user_id, None)

    if not infos:
        return  # Sécurité : ne devrait jamais arriver grâce au filtre

    message_original: Message = infos["message"]
    type_media: str = infos["type"]
    nouveau_nom = message.text.strip()

    if not nouveau_nom:
        await message.reply_text("❌ Le nom ne peut pas être vide. Réessaie.")
        pending_rename[user_id] = infos
        return

    est_video_reelle = type_media == "video" or (type_media == "document" and est_document_video(message_original))

    if not est_video_reelle:
        # Audio ou document non-vidéo classique : pas de menu format/qualité,
        # traitement direct comme auparavant.
        message_statut = await message.reply_text("⏳ Initialisation du téléchargement...")
        format_envoi = "audio" if type_media == "audio" else "document"
        await lancer_traitement_final(
            client=client, user_id=user_id, message_statut=message_statut,
            message_original=message_original, nouveau_nom=nouveau_nom,
            format_envoi=format_envoi, qualite=None,
        )
        return

    # Vidéo ou document vidéo : on propose d'abord le choix du format d'envoi
    pending_traitement[user_id] = {
        "message_original": message_original,
        "type_media": type_media,
        "nouveau_nom": nouveau_nom,
        "format_choisi": None,
    }

    boutons = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎥 Vidéo", callback_data="format_video"),
        InlineKeyboardButton("📁 Document", callback_data="format_document"),
    ]])
    await message.reply_text(
        f"📦 Nouveau nom : `{nouveau_nom}`\n\n**Choisis le format d'envoi :**",
        reply_markup=boutons,
    )


@app.on_callback_query(filters.regex(r"^format_(video|document)$"))
async def callback_choix_format(client: Client, callback_query: CallbackQuery):
    """Étape 1 : l'utilisateur choisit entre envoi en streaming vidéo ou en document brut."""
    user_id = callback_query.from_user.id
    info = pending_traitement.get(user_id)

    if not info:
        await callback_query.answer("⚠️ Session expirée, renvoie le fichier.", show_alert=True)
        return

    format_choisi = callback_query.data.split("_", 1)[1]  # "video" ou "document"
    info["format_choisi"] = format_choisi
    pending_traitement[user_id] = info

    boutons = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Faible (480p) — Gratuit", callback_data="qualite_480p")],
        [InlineKeyboardButton("📺 HD (720p/1080p) — Gratuit", callback_data="qualite_hd")],
        [InlineKeyboardButton("👑 4K Ultra HD — Premium", callback_data="qualite_4k")],
        [InlineKeyboardButton("👑 8K Extreme HD — Premium", callback_data="qualite_8k")],
    ])
    emoji_format = "🎥 Vidéo (streaming)" if format_choisi == "video" else "📁 Document (brut)"

    await callback_query.answer()
    await callback_query.message.edit_text(
        f"✅ Format choisi : {emoji_format}\n\n**Choisis maintenant la qualité :**",
        reply_markup=boutons,
    )


@app.on_callback_query(filters.regex(r"^qualite_"))
async def callback_choix_qualite(client: Client, callback_query: CallbackQuery):
    """Étape 2 : choix de la qualité, avec restriction Premium pour 4K/8K, puis lancement du traitement."""
    user_id = callback_query.from_user.id
    info = pending_traitement.get(user_id)

    if not info or not info.get("format_choisi"):
        await callback_query.answer("⚠️ Session expirée, renvoie le fichier.", show_alert=True)
        return

    qualite = callback_query.data.split("_", 1)[1]  # "480p", "hd", "4k" ou "8k"

    # --------------------------------------------------------------
    # RESTRICTION PREMIUM : 4K et 8K sont réservés aux plans payants
    # --------------------------------------------------------------
    if qualite in QUALITES_PREMIUM:
        doc_utilisateur = await obtenir_ou_creer_utilisateur(user_id, callback_query.from_user.username)
        if doc_utilisateur.get("plan", PLAN_PAR_DEFAUT) == "gratuit":
            await callback_query.answer(
                "🔒 Cette qualité est réservée aux abonnés Premium ! Tapez /plans pour passer au niveau supérieur.",
                show_alert=True,
            )
            return  # Le menu reste affiché, l'utilisateur peut choisir une autre qualité

    # Tout est en ordre : on retire l'état en attente et on démarre le traitement
    info = pending_traitement.pop(user_id)
    await callback_query.answer()

    message_statut = callback_query.message
    await message_statut.edit_text("⏳ Traitement en cours...")

    await lancer_traitement_final(
        client=client, user_id=user_id, message_statut=message_statut,
        message_original=info["message_original"], nouveau_nom=info["nouveau_nom"],
        format_envoi=info["format_choisi"], qualite=qualite,
    )


# ==============================================================================
# 11bis. TRAITEMENT FINAL COMMUN : TÉLÉCHARGEMENT, ENVOI, PUBLICATION, NETTOYAGE
# ==============================================================================

async def lancer_traitement_final(client: Client, user_id: int, message_statut: Message,
                                   message_original: Message, nouveau_nom: str,
                                   format_envoi: str, qualite: str | None):
    """
    Fonction centrale réutilisée par tous les chemins de traitement (audio,
    document classique, ou vidéo/document-vidéo après choix du format et de
    la qualité). Gère : vérification du quota, téléchargement, extraction des
    métadonnées, envoi, auto-suppression après 15 minutes, publication sur les
    canaux, mise à jour des statistiques, et nettoyage disque garanti.

    NOTE IMPORTANTE SUR LA "QUALITÉ" : le fichier source vient déjà encodé
    depuis Telegram — ce bot n'effectue aucun ré-encodage (cela nécessiterait
    ffmpeg et un temps de traitement bien plus long). Le choix de qualité sert
    ici de critère d'accès (gratuit/Premium) et est indiqué dans la légende ;
    la résolution réelle envoyée reste celle du fichier d'origine.
    """
    # ------------------------------------------------------------------
    # VÉRIFICATION DU QUOTA QUOTIDIEN AVANT TOUT TÉLÉCHARGEMENT
    # ------------------------------------------------------------------
    taille_fichier = (
        message_original.video.file_size if message_original.video else
        message_original.audio.file_size if message_original.audio else
        message_original.document.file_size if message_original.document else 0
    )

    autorise, restant, quota_total = await verifier_quota(user_id, taille_fichier)
    if not autorise:
        await message_statut.edit_text(
            "❌ **Quota quotidien dépassé !**\n\n"
            f"📦 Ton quota : {formater_taille(quota_total)}\n"
            f"🟢 Il te reste : {formater_taille(restant)}\n"
            f"📄 Ce fichier fait : {formater_taille(taille_fichier)}\n\n"
            "🚀 Utilise /plans pour découvrir les plans supérieurs et augmenter ta limite quotidienne."
        )
        return

    # Ces deux variables sont déclarées ici pour être accessibles dans le
    # bloc `finally`, qui garantit leur suppression du disque quoi qu'il arrive.
    chemin_telechargement = None
    chemin_final = None

    try:
        # ------------------------------------------------------------------
        # TÉLÉCHARGEMENT HAUTE VITESSE AVEC BARRE DE PROGRESSION TEMPS RÉEL
        # ------------------------------------------------------------------
        suivi_dl = SuiviProgression(message_statut, "Téléchargement")
        chemin_temp = os.path.join(DOSSIER_DOWNLOADS, f"{user_id}_{int(time.time())}")

        chemin_telechargement = await message_original.download(
            file_name=chemin_temp,
            progress=suivi_dl.callback,
        )

        # RENOMMAGE : on déplace le fichier téléchargé vers son nom final
        dossier_parent = os.path.dirname(chemin_telechargement)
        chemin_final = os.path.join(dossier_parent, nouveau_nom)
        shutil.move(chemin_telechargement, chemin_final)
        chemin_telechargement = None  # évite une double tentative de suppression

        # APPLICATION DE LA MINIATURE PERSONNALISÉE (si elle existe)
        infos_thumb = user_thumbnails.get(user_id)
        chemin_thumb = infos_thumb.get("file_path") if infos_thumb else None
        if chemin_thumb and not os.path.isfile(chemin_thumb):
            chemin_thumb = None

        # ------------------------------------------------------------------
        # EXTRACTION DES MÉTADONNÉES RÉELLES VIA HACHOIR (uniquement pour le
        # format Vidéo, comme demandé — le format Document n'a pas besoin de
        # durée/dimensions puisqu'il n'est pas lu en streaming).
        # ------------------------------------------------------------------
        duree_media, largeur_media, hauteur_media = 0, 0, 0
        if format_envoi == "video":
            await message_statut.edit_text("🔎 Analyse des métadonnées du fichier...")
            duree_media, largeur_media, hauteur_media = extraire_metadonnees_video(chemin_final)
        elif format_envoi == "audio":
            duree_media, _, _ = extraire_metadonnees_video(chemin_final)

        # ------------------------------------------------------------------
        # CONSTRUCTION DE LA LÉGENDE (police personnalisée + mention qualité)
        # ------------------------------------------------------------------
        doc_utilisateur = await obtenir_ou_creer_utilisateur(user_id, None)
        style_police = doc_utilisateur.get("font", "normal")
        titre_stylise = nouveau_nom if style_police == "normal" else convertir_police(nouveau_nom, style_police)
        prefixe_titre = f"✅ **{titre_stylise}**" if style_police == "normal" else f"✅ {titre_stylise}"

        note_qualite = f"\n🎞️ Qualité : {NOMS_QUALITES[qualite]}" if qualite else ""

        legende_utilisateur = f"{prefixe_titre}{note_qualite}"
        legende_canal = f"{prefixe_titre}{note_qualite}"  # sans mention de suppression : publication permanente

        # ------------------------------------------------------------------
        # ENVOI HAUTE VITESSE DU FICHIER FINAL À L'UTILISATEUR
        # ------------------------------------------------------------------
        await message_statut.edit_text("📤 Envoi du fichier en cours...")
        suivi_envoi = SuiviProgression(message_statut, "Envoi")

        if format_envoi == "video":
            message_envoye = await client.send_video(
                chat_id=user_id, video=chemin_final, thumb=chemin_thumb, file_name=nouveau_nom,
                caption=legende_utilisateur, duration=duree_media, width=largeur_media,
                height=hauteur_media, progress=suivi_envoi.callback,
            )
        elif format_envoi == "audio":
            message_envoye = await client.send_audio(
                chat_id=user_id, audio=chemin_final, thumb=chemin_thumb, file_name=nouveau_nom,
                caption=legende_utilisateur, duration=duree_media, progress=suivi_envoi.callback,
            )
        else:  # "document" (y compris une vidéo envoyée volontairement en document brut)
            message_envoye = await client.send_document(
                chat_id=user_id, document=chemin_final, thumb=chemin_thumb, file_name=nouveau_nom,
                caption=legende_utilisateur, progress=suivi_envoi.callback,
            )

        # Suppression du message d'attente : on ne laisse plus de message "en cours" affiché
        try:
            await message_statut.delete()
        except Exception:
            pass

        # ------------------------------------------------------------------
        # PLANIFICATION DE LA SUPPRESSION AUTOMATIQUE DU MESSAGE (15 minutes)
        # Tâche indépendante : ne bloque pas la suite du traitement (canaux, etc.)
        # ------------------------------------------------------------------
        asyncio.create_task(supprimer_message_apres_delai(client, user_id, message_envoye.id))

        # Mise à jour de la consommation quotidienne et des statistiques globales
        await ajouter_usage(user_id, taille_fichier)

        # ------------------------------------------------------------------
        # PUBLICATION AUTOMATIQUE SUR TOUS LES CANAUX ENREGISTRÉS
        # (copie sans la mention de suppression : les posts de canal restent
        # permanents et ne sont PAS concernés par l'auto-suppression 15 min)
        # ------------------------------------------------------------------
        canaux_actuels = user_channels.get(user_id, [])

        if canaux_actuels:
            rapport = "📡 **Résultat de la publication sur tes canaux :**\n\n"
            for canal in canaux_actuels:
                try:
                    await message_envoye.copy(chat_id=canal["id"], caption=legende_canal)
                    rapport += f"✅ {canal['title']}\n"
                except ChatAdminRequired:
                    rapport += f"❌ {canal['title']} — je ne suis plus administrateur.\n"
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    try:
                        await message_envoye.copy(chat_id=canal["id"], caption=legende_canal)
                        rapport += f"✅ {canal['title']} (après attente)\n"
                    except Exception as erreur2:
                        rapport += f"❌ {canal['title']} — erreur : `{erreur2}`\n"
                except RPCError as erreur:
                    rapport += f"❌ {canal['title']} — erreur : `{erreur}`\n"

            await client.send_message(user_id, rapport)
        else:
            await client.send_message(
                user_id,
                "ℹ️ Aucun canal enregistré : le fichier n'a pas été publié automatiquement. "
                "Utilise /ajouter_canal pour en enregistrer.",
            )

    except FloodWait as e:
        await client.send_message(user_id, f"⏳ Limite Telegram atteinte, réessaie dans {e.value} secondes.")

    except Exception as erreur:
        logger.exception("Erreur pendant le traitement du fichier")
        try:
            await message_statut.edit_text(
                f"❌ **Une erreur est survenue :**\n`{erreur}`\n\n"
                "Vérifie que le fichier n'est pas trop volumineux et que mes permissions "
                "sur les canaux sont correctes."
            )
        except Exception:
            await client.send_message(user_id, f"❌ **Une erreur est survenue :**\n`{erreur}`")

    finally:
        # ------------------------------------------------------------------
        # NETTOYAGE DISQUE GARANTI (s'exécute même en cas d'erreur) : le
        # fichier source téléchargé et/ou le fichier renommé final sont
        # systématiquement supprimés — aucune donnée temporaire ne subsiste
        # sur le serveur (Railway ou autre hébergeur).
        # ------------------------------------------------------------------
        nettoyer_fichiers(chemin_telechargement, chemin_final)


# ==============================================================================
# 13. ABONNEMENT OBLIGATOIRE : GATE GLOBAL + CALLBACK DE VÉRIFICATION
# ==============================================================================

@app.on_message(filters.private, group=-1)
async def intercepteur_verification_abonnement(client: Client, message: Message):
    """
    Gate global exécuté AVANT tout autre handler (group=-1) : vérifie que
    l'utilisateur est abonné au canal obligatoire pour n'importe quel message
    ou fichier envoyé au bot. Si non abonné, le message est bloqué ici (aucun
    autre handler ne s'exécute) et un message avec bouton "Vérifier" est envoyé.

    IMPORTANT : `continue_propagation()` ne doit JAMAIS être englobé dans un
    `try/except` générique — cette méthode fonctionne en levant une exception
    interne que le dispatcher de Pyrogram intercepte spécifiquement pour
    savoir qu'il doit continuer vers les autres groupes de handlers. La
    capturer par erreur (comme dans une version précédente de ce fichier)
    bloquerait silencieusement TOUS les autres handlers.
    """
    if not message.from_user:
        return

    user_id = message.from_user.id

    # L'administrateur n'est jamais soumis à la vérification d'abonnement
    if user_id == ADMIN_ID:
        message.continue_propagation()
        return

    abonne = await verifier_abonnement_canal(client, user_id)

    if abonne:
        message.continue_propagation()
    else:
        await envoyer_message_abonnement_requis(client, user_id)
        # Pas d'appel à continue_propagation() : le message est bloqué ici.


@app.on_callback_query(group=-1)
async def intercepteur_verification_abonnement_callback(client: Client, callback_query: CallbackQuery):
    """
    Équivalent du gate ci-dessus, mais pour les clics sur les boutons inline
    (choix de format, de qualité, de police, etc.). Le bouton "✅ Vérifier"
    lui-même est toujours exempté, sinon un utilisateur non abonné ne pourrait
    jamais débloquer son accès.
    """
    user_id = callback_query.from_user.id

    if user_id == ADMIN_ID or callback_query.data == "verifier_abonnement":
        callback_query.continue_propagation()
        return

    abonne = await verifier_abonnement_canal(client, user_id)

    if abonne:
        callback_query.continue_propagation()
    else:
        await callback_query.answer(
            "🔒 Tu dois d'abord rejoindre notre canal pour utiliser cette fonctionnalité. "
            "Tape /start pour voir le bouton d'inscription.",
            show_alert=True,
        )


@app.on_callback_query(filters.regex(r"^verifier_abonnement$"))
async def callback_verifier_abonnement(client: Client, callback_query: CallbackQuery):
    """Traite le clic sur '✅ Vérifier' : re-teste l'abonnement et débloque l'accès si confirmé."""
    user_id = callback_query.from_user.id
    abonne = await verifier_abonnement_canal(client, user_id)

    if abonne:
        await callback_query.answer("✅ Abonnement vérifié, accès débloqué !", show_alert=True)
        try:
            await callback_query.message.edit_text(
                "✅ **Accès débloqué !**\n\nTu peux maintenant utiliser le bot normalement. Tape /start pour commencer."
            )
        except Exception:
            pass
    else:
        await callback_query.answer(
            "❌ Tu n'es toujours pas abonné au canal. Rejoins-le puis réessaie.",
            show_alert=True,
        )


# ==============================================================================
# 14. DÉMARRAGE DU BOT (démarrage asynchrone + chargement MongoDB avant idle)
# ==============================================================================

async def main():
    async with app:
        print("=" * 60)
        print(" BOT RENAME & POST v2 - Démarrage en cours...")
        print(f" Heure de démarrage : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        # Rechargement automatique des canaux et miniatures depuis MongoDB Atlas
        await mongo_charger_toutes_donnees(app)

        logger.info("Bot opérationnel et en écoute des messages.")
        await idle()  # Maintient le bot actif jusqu'à Ctrl+C / arrêt du processus


if __name__ == "__main__":
    app.run(main())
