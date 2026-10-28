package com.pff; // même paquet que java-libpst pour lire les propriétés MAPI brutes (getStringItem)

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.text.SimpleDateFormat;
import java.util.*;

/**
 * Extraction en lecture seule d'un PST.
 *   PstExport <fichier.pst> <dossier-sortie> <fuseau-par-défaut>
 * Produit <sortie>/items.jsonl (une ligne JSON par dossier / élément) et <sortie>/att/ (pièces jointes).
 * Progression sur stderr : "PROGRESS <n>".
 */
public class PstExport {
    static final long EPOCH_1601_MS = 11644473600000L;
    static final int PR_SENDER_SMTP_ADDRESS = 0x5D01;
    static final int PR_SENT_REPRESENTING_SMTP_ADDRESS = 0x5D02;

    static PSTFile pst;
    static Path attDir;
    static Writer out;
    static TimeZone defaultTz;
    static int attSeq = 0, items = 0, errors = 0;

    public static void main(String[] a) throws Exception {
        if (a.length != 3) {
            System.err.println("usage: PstExport <pst> <dossier-sortie> <fuseau>");
            System.exit(2);
        }
        Path dir = Paths.get(a[1]);
        attDir = dir.resolve("att");
        Files.createDirectories(attDir);
        defaultTz = TimeZone.getTimeZone(a[2]);
        pst = new PSTFile(new File(a[0]));
        try (Writer w = new BufferedWriter(new OutputStreamWriter(
                new FileOutputStream(dir.resolve("items.jsonl").toFile()), StandardCharsets.UTF_8))) {
            out = w;
            walk(pst.getRootFolder(), new ArrayList<>());
            line(new J().put("kind", "end").put("items", items).put("errors", errors));
        } finally {
            pst.close();
        }
    }

    // --- Parcours --------------------------------------------------------------

    static void walk(PSTFolder f, List<String> path) throws Exception {
        if (!path.isEmpty()) {
            line(new J().put("kind", "folder").put("path", path).put("class", f.getContainerClass())
                    .put("count", f.getContentCount()).put("ftype", f.getFolderType()));
        }
        if (f.getContentCount() > 0) {
            for (Integer node : f.getChildDescriptorNodes()) {
                try {
                    PSTObject o = PSTObject.detectAndLoadPSTObject(pst, node);
                    J j = item(o);
                    j.put("path", path).put("node", (long) node);
                    line(j);
                } catch (Exception e) {
                    errors++;
                    line(new J().put("kind", "error").put("path", path).put("node", (long) node).put("error", e.toString()));
                }
                if (++items % 100 == 0) {
                    System.err.println("PROGRESS " + items);
                    System.err.flush();
                }
            }
        }
        if (f.hasSubfolders()) {
            for (PSTFolder c : f.getSubFolders()) {
                List<String> p = new ArrayList<>(path);
                p.add(c.getDisplayName());
                walk(c, p);
            }
        }
    }

    static J item(PSTObject o) throws Exception {
        String cls = nz(o.getMessageClass());
        String uc = cls.toUpperCase(Locale.ROOT);
        if (o instanceof PSTDistList || uc.startsWith("IPM.DISTLIST"))
            return new J().put("kind", "distlist").put("class", cls).put("name", o.getDisplayName());
        if (o instanceof PSTContact && uc.startsWith("IPM.CONTACT"))
            return contact((PSTContact) o).put("kind", "contact").put("class", cls);
        if (o instanceof PSTTask && uc.startsWith("IPM.TASK") && !uc.startsWith("IPM.TASKREQUEST"))
            return task((PSTTask) o).put("kind", "task").put("class", cls);
        if (o instanceof PSTAppointment && uc.startsWith("IPM.APPOINTMENT"))
            return appointment((PSTAppointment) o).put("kind", "appt").put("class", cls);
        if (uc.startsWith("IPM.STICKYNOTE") || uc.startsWith("IPM.ACTIVITY") || !(o instanceof PSTMessage))
            return new J().put("kind", "other").put("class", cls).put("subject", o.getDisplayName());
        return message((PSTMessage) o).put("kind", "mail").put("class", cls);
    }

    // --- Messages --------------------------------------------------------------

    static J message(PSTMessage m) throws Exception {
        J j = new J()
            .put("subject", m.getSubject())
            .put("headers", safe(m::getTransportMessageHeaders))
            .put("msgid", safe(m::getInternetMessageId))
            .put("in_reply_to", safe(m::getInReplyToId))
            .put("read", m.isRead())
            .put("flagged", safe(m::isFlagged))
            .put("replied", safe(m::hasReplied))
            .put("forwarded", safe(m::hasForwarded))
            .put("unsent", safe(m::isUnsent))
            .put("importance", m.getImportance())
            .put("submit_ms", ms(m.getClientSubmitTime()))
            .put("delivery_ms", ms(m.getMessageDeliveryTime()))
            .put("sender_name", nz(m.getSenderName()).isEmpty() ? m.getSentRepresentingName() : m.getSenderName())
            .put("sender_smtp", senderSmtp(m))
            .put("body", safe(m::getBody))
            .put("html", safe(m::getBodyHTML))
            .put("recipients", recipients(m));
        List<Object> atts = new ArrayList<>();
        for (int i = 0; i < m.getNumberOfAttachments(); i++) {
            try {
                atts.add(attachment(m.getAttachment(i)));
            } catch (Exception e) {
                atts.add(new J().put("error", e.toString()));
            }
        }
        j.put("attachments", atts);
        return j;
    }

    static String senderSmtp(PSTMessage m) {
        String s = safe(() -> m.getStringItem(PR_SENDER_SMTP_ADDRESS));
        if (!nz(s).isEmpty()) return s;
        if ("SMTP".equalsIgnoreCase(m.getSenderAddrtype())) return m.getSenderEmailAddress();
        s = safe(() -> m.getStringItem(PR_SENT_REPRESENTING_SMTP_ADDRESS));
        if (!nz(s).isEmpty()) return s;
        if ("SMTP".equalsIgnoreCase(m.getSentRepresentingAddressType())) return m.getSentRepresentingEmailAddress();
        return "";
    }

    static List<Object> recipients(PSTMessage m) throws Exception {
        List<Object> l = new ArrayList<>();
        for (int i = 0; i < m.getNumberOfRecipients(); i++) {
            PSTRecipient r = m.getRecipient(i);
            String smtp = nz(r.getSmtpAddress());
            if (smtp.isEmpty() && "SMTP".equalsIgnoreCase(r.getEmailAddressType())) smtp = nz(r.getEmailAddress());
            l.add(new J().put("name", r.getDisplayName()).put("smtp", smtp).put("type", r.getRecipientType()));
        }
        return l;
    }

    static J attachment(PSTAttachment at) throws Exception {
        String name = nz(at.getLongFilename());
        if (name.isEmpty()) name = nz(at.getFilename());
        if (name.isEmpty()) name = nz(at.getDisplayName());
        J a = new J().put("name", name).put("mime", at.getMimeTag()).put("cid", at.getContentId())
                .put("method", at.getAttachMethod());
        if (at.getAttachMethod() == PSTAttachment.ATTACHMENT_METHOD_EMBEDDED) {
            PSTMessage em = at.getEmbeddedPSTMessage();
            if (em != null) a.put("message", message(em));
            return a;
        }
        try (InputStream in = at.getFileInputStream()) {
            if (in != null) {
                Path f = attDir.resolve("a" + (++attSeq));
                Files.copy(in, f);
                a.put("file", "att/" + f.getFileName());
            }
        }
        return a;
    }

    // --- Rendez-vous -----------------------------------------------------------

    static J appointment(PSTAppointment ap) throws Exception {
        J j = new J()
            .put("subject", ap.getSubject())
            .put("location", ap.getLocation())
            .put("body", safe(ap::getBody))
            .put("start_ms", ms(ap.getStartTime()))
            .put("end_ms", ms(ap.getEndTime()))
            .put("allday", ap.getSubType())
            .put("busy", ap.getBusyStatus())
            .put("sensitivity", ap.getSensitivity())
            .put("reminder_set", safe(ap::getReminderSet))
            .put("reminder_min", safe(ap::getReminderDelta))
            .put("meeting_status", ap.getMeetingStatus())
            .put("organizer_name", nz(ap.getSentRepresentingName()).isEmpty() ? ap.getSenderName() : ap.getSentRepresentingName())
            .put("organizer_smtp", senderSmtp(ap))
            .put("attendees", recipients(ap));

        PSTTimeZone ptz = ap.getRecurrenceTimeZone();
        if (ptz == null) ptz = safe(ap::getStartTimeZone);
        TimeZone tz = (ptz != null && ptz.getSimpleTimeZone() != null) ? ptz.getSimpleTimeZone() : defaultTz;
        if (ap.getSubType()) {
            j.put("start_date", fmt(ap.getStartTime(), "yyyyMMdd", tz)).put("end_date", fmt(ap.getEndTime(), "yyyyMMdd", tz));
        }
        if (ap.isRecurring() && ap.getRecurrenceStructure() != null) {
            try {
                j.put("rec", recurrence(ap, ptz, tz));
            } catch (Exception e) {
                j.put("rec_error", e.toString());
            }
        }
        return j;
    }

    static J recurrence(PSTAppointment ap, PSTTimeZone ptz, TimeZone tz) throws Exception {
        PSTAppointmentRecurrence r = new PSTAppointmentRecurrence(ap.getRecurrenceStructure(), ap, ptz);
        Set<String> modified = new HashSet<>();
        if (r.getModifiedInstanceDates() != null)
            for (Calendar c : r.getModifiedInstanceDates()) modified.add(fmt(c.getTime(), "yyyyMMdd", tz));
        List<Object> deleted = new ArrayList<>();
        if (r.getDeletedInstanceDates() != null)
            for (Calendar c : r.getDeletedInstanceDates()) {
                String d = fmt(c.getTime(), "yyyyMMdd", tz);
                if (!modified.contains(d)) deleted.add(d);
            }
        List<Object> exc = new ArrayList<>();
        for (int i = 0; i < r.getExceptionCount(); i++) {
            PSTAppointmentException e = r.getException(i);
            exc.add(new J().put("orig_local", local(e.getOriginalStartDate())).put("start_local", local(e.getStartDateTime()))
                    .put("end_local", local(e.getEndDateTime())).put("subject", safe(e::getSubject)).put("location", safe(e::getLocation)));
        }
        J j = new J()
            .put("freq", r.getRecurFrequency()).put("ptype", (int) r.getPatternType()).put("period", r.getPeriod())
            .put("spec", r.getPatternSpecific()).put("nth", r.getPatternSpecificNth()).put("firstdow", r.getFirstDOW())
            .put("end_type", r.getEndType()).put("count", r.getOccurrenceCount())
            .put("end_date", local(r.getEndDate()).substring(0, 8))
            .put("calendar_type", (int) r.getCalendarType())
            .put("start_local", fmt(ap.getStartTime(), "yyyyMMdd'T'HHmmss", tz))
            .put("end_local", fmt(ap.getEndTime(), "yyyyMMdd'T'HHmmss", tz))
            .put("start_offset", r.getStartTimeOffset())
            .put("exdates", deleted).put("exceptions", exc)
            .put("tz", tzJson(ptz, tz));
        return j;
    }

    static J tzJson(PSTTimeZone p, TimeZone tz) {
        if (p == null) {
            return new J().put("name", tz.getID()).put("bias", -tz.getRawOffset() / 60000).put("std_bias", 0).put("dst_bias", 0);
        }
        return new J().put("name", p.getName()).put("bias", p.getBias()).put("std_bias", p.getStandardBias())
                .put("dst_bias", p.getDaylightBias()).put("std", sys(p.getStandardStart())).put("dst", sys(p.getDaylightStart()));
    }

    static J sys(PSTTimeZone.SYSTEMTIME s) {
        if (s == null || s.wMonth == 0) return null;
        return new J().put("month", (int) s.wMonth).put("dow", (int) s.wDayOfWeek).put("week", (int) s.wDay)
                .put("hour", (int) s.wHour).put("minute", (int) s.wMinute);
    }

    /** Minutes depuis 1601 en heure locale -> "yyyyMMddTHHmmss" (heure murale). */
    static String local(int minutes) {
        return fmt(new Date((minutes & 0xFFFFFFFFL) * 60000L - EPOCH_1601_MS), "yyyyMMdd'T'HHmmss", TimeZone.getTimeZone("UTC"));
    }

    // --- Contacts et tâches ----------------------------------------------------

    static J contact(PSTContact c) throws Exception {
        String smtp = nz(c.getSMTPAddress());
        return new J()
            .put("display", c.getDisplayName()).put("given", c.getGivenName()).put("surname", c.getSurname())
            .put("middle", c.getMiddleName()).put("prefix", c.getDisplayNamePrefix()).put("suffix", c.getGeneration())
            .put("fileas", c.getFileUnder()).put("nickname", c.getNickname())
            .put("company", c.getCompanyName()).put("department", c.getDepartmentName()).put("title", c.getTitle())
            .put("email1", email(c.getEmail1EmailAddress(), c.getEmail1AddressType(), smtp))
            .put("email2", email(c.getEmail2EmailAddress(), c.getEmail2AddressType(), ""))
            .put("email3", email(c.getEmail3EmailAddress(), c.getEmail3AddressType(), ""))
            .put("tel_work", c.getBusinessTelephoneNumber()).put("tel_work2", c.getBusiness2TelephoneNumber())
            .put("tel_home", c.getHomeTelephoneNumber()).put("tel_home2", c.getHome2TelephoneNumber())
            .put("tel_mobile", c.getMobileTelephoneNumber()).put("tel_other", c.getOtherTelephoneNumber())
            .put("tel_company", c.getCompanyMainPhoneNumber()).put("tel_assistant", c.getAssistantTelephoneNumber())
            .put("tel_pager", c.getPagerTelephoneNumber())
            .put("fax_work", c.getBusinessFaxNumber()).put("fax_home", c.getHomeFaxNumber())
            .put("work_street", c.getWorkAddressStreet()).put("work_city", c.getWorkAddressCity())
            .put("work_state", c.getWorkAddressState()).put("work_postal", c.getWorkAddressPostalCode())
            .put("work_country", c.getWorkAddressCountry())
            .put("home_street", c.getHomeAddressStreet()).put("home_city", c.getHomeAddressCity())
            .put("home_state", c.getHomeAddressStateOrProvince()).put("home_postal", c.getHomeAddressPostalCode())
            .put("home_country", c.getHomeAddressCountry())
            .put("other_street", c.getOtherAddressStreet()).put("other_city", c.getOtherAddressCity())
            .put("other_state", c.getOtherAddressStateOrProvince()).put("other_postal", c.getOtherAddressPostalCode())
            .put("other_country", c.getOtherAddressCountry())
            .put("url_work", c.getBusinessHomePage()).put("url_home", c.getPersonalHomePage())
            .put("im", c.getInstantMessagingAddress())
            .put("birthday", c.getBirthday() == null ? null : fmt(c.getBirthday(), "yyyy-MM-dd", defaultTz))
            .put("anniversary", c.getAnniversary() == null ? null : fmt(c.getAnniversary(), "yyyy-MM-dd", defaultTz))
            .put("note", safe(c::getBody));
    }

    static String email(String addr, String type, String fallback) {
        if (addr != null && !addr.isEmpty() && (type == null || type.isEmpty() || "SMTP".equalsIgnoreCase(type))) return addr;
        return fallback.isEmpty() ? null : fallback;
    }

    static J task(PSTTask t) throws Exception {
        return new J()
            .put("subject", t.getSubject()).put("body", safe(t::getBody))
            .put("status", t.getTaskStatus()).put("percent", t.getPercentComplete())
            .put("complete", t.isTaskComplete()).put("importance", t.getImportance())
            .put("start", t.getTaskStartDate() == null ? null : fmt(t.getTaskStartDate(), "yyyyMMdd", defaultTz))
            .put("due", t.getTaskDueDate() == null ? null : fmt(t.getTaskDueDate(), "yyyyMMdd", defaultTz))
            .put("completed_ms", ms(t.getTaskDateCompleted()));
    }

    // --- Utilitaires -------------------------------------------------------------

    interface X<T> { T get() throws Exception; }

    static <T> T safe(X<T> x) {
        try { return x.get(); } catch (Exception e) { return null; }
    }

    static String nz(String s) { return s == null ? "" : s; }

    static Long ms(Date d) { return d == null ? null : d.getTime(); }

    static String fmt(Date d, String pattern, TimeZone tz) {
        SimpleDateFormat f = new SimpleDateFormat(pattern, Locale.ROOT);
        f.setTimeZone(tz);
        return f.format(d);
    }

    static void line(J j) throws IOException {
        out.write(j.toString());
        out.write('\n');
    }

    /** Objet JSON minimal (les valeurs nulles sont omises). */
    static class J {
        final LinkedHashMap<String, Object> m = new LinkedHashMap<>();

        J put(String k, Object v) {
            if (v != null) m.put(k, v);
            return this;
        }

        public String toString() {
            StringBuilder b = new StringBuilder();
            write(b, this);
            return b.toString();
        }

        static void write(StringBuilder b, Object v) {
            if (v == null) b.append("null");
            else if (v instanceof J) {
                b.append('{');
                boolean first = true;
                for (Map.Entry<String, Object> e : ((J) v).m.entrySet()) {
                    if (!first) b.append(',');
                    first = false;
                    str(b, e.getKey());
                    b.append(':');
                    write(b, e.getValue());
                }
                b.append('}');
            } else if (v instanceof List) {
                b.append('[');
                boolean first = true;
                for (Object o : (List<?>) v) {
                    if (!first) b.append(',');
                    first = false;
                    write(b, o);
                }
                b.append(']');
            } else if (v instanceof Boolean || v instanceof Integer || v instanceof Long) b.append(v);
            else if (v instanceof Double) {
                double d = (Double) v;
                b.append(Double.isFinite(d) ? Double.toString(d) : "null");
            } else str(b, v.toString());
        }

        static void str(StringBuilder b, String s) {
            b.append('"');
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    default:
                        if (c < 0x20 || Character.isSurrogate(c) && !validSurrogate(s, i)) b.append(String.format("\\u%04x", (int) c));
                        else b.append(c);
                }
            }
            b.append('"');
        }

        static boolean validSurrogate(String s, int i) {
            char c = s.charAt(i);
            if (Character.isHighSurrogate(c)) return i + 1 < s.length() && Character.isLowSurrogate(s.charAt(i + 1));
            return i > 0 && Character.isHighSurrogate(s.charAt(i - 1));
        }
    }
}
