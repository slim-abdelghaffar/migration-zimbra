package com.pff; // même paquet que java-libpst pour lire PR_SENDER_SMTP_ADDRESS (getStringItem est protégé)

import java.text.SimpleDateFormat;
import java.util.*;

/** Analyse en lecture seule d'un PST : une ligne TSV par élément, sur la sortie standard. */
public class PstDump {
    static final SimpleDateFormat ISO = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX");

    public static void main(String[] args) throws Exception {
        ISO.setTimeZone(TimeZone.getTimeZone("UTC"));
        PSTFile pst = new PSTFile(args[0]);
        System.out.println("#type\tdossier\tclasse\tlu\tmessage_id\tdate_envoi\tdate_reception\texp_type\texp_adresse\texp_smtp\trepr_smtp\textra");
        walk(pst.getRootFolder(), "");
        pst.close();
    }

    static String d(Date x) { return x == null ? "" : ISO.format(x); }
    static String s(String x) { return x == null ? "" : x.replace('\t', ' ').replace('\n', ' ').replace('\r', ' '); }

    static void walk(PSTFolder f, String path) throws Exception {
        String p = path.isEmpty() ? f.getDisplayName() : path + "/" + f.getDisplayName();
        System.out.println("FOLDER\t" + s(p) + "\t" + s(f.getContainerClass()) + "\t" + f.getContentCount());
        if (f.getContentCount() > 0) {
            PSTObject o;
            while ((o = f.getNextChild()) != null) {
                try { item(o, p); } catch (Exception e) { System.out.println("ERROR\t" + s(p) + "\t" + s(e.toString())); }
            }
        }
        if (f.hasSubfolders()) for (PSTFolder c : f.getSubFolders()) walk(c, p);
    }

    static void item(PSTObject o, String p) throws Exception {
        String extra = "";
        String type = "MSG";
        if (o instanceof PSTContact) {
            PSTContact c = (PSTContact) o;
            type = "CONTACT";
            extra = "nom=" + s(c.getDisplayName()) + " email1=" + s(c.getEmail1EmailAddress())
                  + " type1=" + s(c.getEmail1AddressType()) + " smtp=" + s(c.getSMTPAddress());
        } else if (o instanceof PSTAppointment) {
            PSTAppointment a = (PSTAppointment) o;
            type = "APPT";
            StringBuilder att = new StringBuilder();
            for (int i = 0; i < a.getNumberOfRecipients(); i++) {
                PSTRecipient r = a.getRecipient(i);
                att.append(s(r.getSmtpAddress())).append(i + 1 < a.getNumberOfRecipients() ? "," : "");
            }
            extra = "debut=" + d(a.getStartTime()) + " fin=" + d(a.getEndTime()) + " recurrent=" + a.isRecurring()
                  + " motif=" + s(a.getRecurrencePattern()) + " participants=" + att;
        }
        if (!(o instanceof PSTMessage)) { System.out.println("OTHER\t" + s(p) + "\t" + o.getClass().getSimpleName()); return; }
        PSTMessage m = (PSTMessage) o;
        System.out.println(type + "\t" + s(p) + "\t" + s(m.getMessageClass()) + "\t" + m.isRead()
            + "\t" + s(m.getInternetMessageId()) + "\t" + d(m.getClientSubmitTime()) + "\t" + d(m.getMessageDeliveryTime())
            + "\t" + s(m.getSenderAddrtype()) + "\t" + s(m.getSenderEmailAddress())
            + "\t" + s(m.getStringItem(0x5D01)) + "\t" + s(m.getStringItem(0x5D02)) + "\t" + extra);
    }
}
