public with sharing class SampleAction {
    @InvocableMethod(label='Run synthetic example')
    public static void run(List<Account> records) {
        List<Contact> contacts = [SELECT Id, LastName FROM Contact WHERE AccountId != null];
        update records;
    }

    public static void beforeUpdate(List<Account> records) {
        normalize(records);
    }

    public static void normalize(List<Account> records) {
        for (Account record : records) {
            record.Description = record.Name;
        }
    }
}
