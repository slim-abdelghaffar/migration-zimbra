package com.pff;

import java.io.File;
import java.text.Normalizer;
import java.util.*;

/**
 * Identification rapide (lecture seule) du propriétaire d'un PST, par son contenu :
 *   PstIdentify <fichier.pst>
 * Écrit sur stdout un objet JSON : nom du magasin, expéditeurs des Éléments envoyés,
 * destinataires (« reçu par ») de la Boîte de réception, nombre total d'éléments.
 */
public class PstIdentify {
    static final int MAX_PER_FOLDER = 300;
    static final int PR_RECEIVED_BY_SMTP_ADDRESS = 0x5D07;
    static final Set<String> SENT = new HashSet<>(Arrays.asList(
        "éléments envoyés", "elements envoyes", "sent items", "sent", "messages envoyés", "envoyés"));
    static final Set<String> INBOX = new HashSet<>(Arrays.asList(
        "boîte de réception", "boite de reception", "inbox", "courrier entrant"));

    static final Map<String, Integer> sent = new HashMap<>(), received = new HashMap<>();
    static int total = 0;

    public static void main(String[] a) throws Exception {
        PSTFile pst = new PSTFile(new File(a[0]));
        String store = "";
        try {
            store = PstExport.safe(() -> pst.getMessageStore().getDisplayName());
            walk(pst.getRootFolder(), 0, false, false);
        } finally {
            pst.close();
        }
        System.out.println(new PstExport.J().put("store", store).put("total", total)
            .put("sent", top(sent)).put("received", top(received)));
    }

    static String norm(String s) {
        return Normalizer.normalize(PstExport.nz(s).trim().toLowerCase(Locale.ROOT), Normalizer.Form.NFC);
    }

    static int sentSeen = 0, inboxSeen = 0;

    /** Parcourt l'arborescence ; les sous-dossiers des Éléments envoyés / de la Boîte de réception sont inclus. */
    static void walk(PSTFolder f, int depth, boolean inSent, boolean inInbox) throws Exception {
        total += f.getContentCount();
        String n = norm(f.getDisplayName());
        boolean isSent = inSent || depth <= 2 && SENT.contains(n);
        boolean isInbox = !isSent && (inInbox || depth <= 2 && INBOX.contains(n));
        if ((isSent || isInbox) && f.getContentCount() > 0) {
            for (Integer node : f.getChildDescriptorNodes()) {
                if (isSent ? sentSeen++ >= MAX_PER_FOLDER : inboxSeen++ >= MAX_PER_FOLDER) break;
                try {
                    PSTObject o = PSTObject.detectAndLoadPSTObject(pst(f), node);
                    if (!(o instanceof PSTMessage)) continue;
                    PSTMessage m = (PSTMessage) o;
                    if (isSent) add(sent, PstExport.senderSmtp(m));
                    else {
                        String r = PstExport.safe(() -> m.getStringItem(PR_RECEIVED_BY_SMTP_ADDRESS));
                        if (PstExport.nz(r).isEmpty() && "SMTP".equalsIgnoreCase(m.getReceivedByAddressType()))
                            r = m.getReceivedByAddress();
                        if (PstExport.nz(r).isEmpty()) {  // sinon : destinataires directs (To)
                            for (int i = 0; i < m.getNumberOfRecipients(); i++) {
                                PSTRecipient rc = m.getRecipient(i);
                                if (rc.getRecipientType() == 1) add(received, PstExport.nz(rc.getSmtpAddress()).isEmpty()
                                        && "SMTP".equalsIgnoreCase(rc.getEmailAddressType()) ? rc.getEmailAddress() : rc.getSmtpAddress());
                            }
                        } else add(received, r);
                    }
                } catch (Exception e) {
                    // élément illisible : ignoré pour l'identification
                }
            }
        }
        if (f.hasSubfolders()) for (PSTFolder c : f.getSubFolders()) walk(c, depth + 1, isSent, isInbox);
    }

    static PSTFile pst(PSTFolder f) { return f.pstFile; }

    static void add(Map<String, Integer> m, String addr) {
        String a = PstExport.nz(addr).trim().toLowerCase(Locale.ROOT);
        if (a.contains("@")) m.merge(a, 1, Integer::sum);
    }

    static List<Object> top(Map<String, Integer> m) {
        List<Map.Entry<String, Integer>> l = new ArrayList<>(m.entrySet());
        l.sort((x, y) -> y.getValue() - x.getValue());
        List<Object> out = new ArrayList<>();
        for (int i = 0; i < Math.min(5, l.size()); i++)
            out.add(new PstExport.J().put("addr", l.get(i).getKey()).put("n", l.get(i).getValue()));
        return out;
    }
}
